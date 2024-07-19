import os
from io import BytesIO
from pathlib import Path
from pkgutil import get_data
from subprocess import Popen
from unittest import mock

import pytest
from scrapy.utils.test import get_pythonpath
from twisted.trial import unittest
from twisted.web import error

from scrapyd import get_application
from scrapyd.exceptions import DirectoryTraversalError, RunnerError
from scrapyd.interfaces import IEggStorage
from scrapyd.jobstorage import Job
from scrapyd.webservice import UtilsCache, get_spider_list


def fake_list_jobs(*args, **kwargs):
    yield Job("proj1", "spider-a", "id1234")


def fake_list_spiders(*args, **kwargs):
    return []


def fake_list_spiders_other(*args, **kwargs):
    return ["quotesbot", "toscrape-css"]


def get_pythonpath_scrapyd():
    scrapyd_path = __import__("scrapyd").__path__[0]
    return os.path.join(os.path.dirname(scrapyd_path), get_pythonpath(), os.environ.get("PYTHONPATH", ""))


class GetSpiderListTest(unittest.TestCase):
    def setUp(self):
        path = os.path.abspath(self.mktemp())
        j = os.path.join
        eggs_dir = j(path, "eggs")
        os.makedirs(eggs_dir)
        dbs_dir = j(path, "dbs")
        os.makedirs(dbs_dir)
        logs_dir = j(path, "logs")
        os.makedirs(logs_dir)
        os.chdir(path)
        with open("scrapyd.conf", "w") as f:
            f.write("[scrapyd]\n")
            f.write(f"eggs_dir = {eggs_dir}\n")
            f.write(f"dbs_dir = {dbs_dir}\n")
            f.write(f"logs_dir = {logs_dir}\n")
        self.app = get_application()

    def add_test_version(self, file, project, version):
        eggstorage = self.app.getComponent(IEggStorage)
        eggfile = BytesIO(get_data("tests", file))
        eggstorage.put(eggfile, project, version)

    def test_get_spider_list_log_stdout(self):
        self.add_test_version("logstdout.egg", "logstdout", "logstdout")
        spiders = get_spider_list("logstdout", pythonpath=get_pythonpath_scrapyd())
        # If LOG_STDOUT were respected, the output would be [].
        self.assertEqual(sorted(spiders), ["spider1", "spider2"])

    def test_get_spider_list(self):
        # mybot.egg has two spiders, spider1 and spider2
        self.add_test_version("mybot.egg", "mybot", "r1")
        spiders = get_spider_list("mybot", pythonpath=get_pythonpath_scrapyd())
        self.assertEqual(sorted(spiders), ["spider1", "spider2"])

        # mybot2.egg has three spiders, spider1, spider2 and spider3...
        # BUT you won't see it here because it's cached.
        # Effectivelly it's like if version was never added
        self.add_test_version("mybot2.egg", "mybot", "r2")
        spiders = get_spider_list("mybot", pythonpath=get_pythonpath_scrapyd())
        self.assertEqual(sorted(spiders), ["spider1", "spider2"])

        # Let's invalidate the cache for this project...
        UtilsCache.invalid_cache("mybot")

        # Now you get the updated list
        spiders = get_spider_list("mybot", pythonpath=get_pythonpath_scrapyd())
        self.assertEqual(sorted(spiders), ["spider1", "spider2", "spider3"])

        # Let's re-deploy mybot.egg and clear cache. It now sees 2 spiders
        self.add_test_version("mybot.egg", "mybot", "r3")
        UtilsCache.invalid_cache("mybot")
        spiders = get_spider_list("mybot", pythonpath=get_pythonpath_scrapyd())
        self.assertEqual(sorted(spiders), ["spider1", "spider2"])

        # And re-deploying the one with three (mybot2.egg) with a version that
        # isn't the higher, won't change what get_spider_list() returns.
        self.add_test_version("mybot2.egg", "mybot", "r1a")
        UtilsCache.invalid_cache("mybot")
        spiders = get_spider_list("mybot", pythonpath=get_pythonpath_scrapyd())
        self.assertEqual(sorted(spiders), ["spider1", "spider2"])

    @pytest.mark.skipif(os.name == "nt", reason="get_spider_list() unicode fails on windows")
    def test_get_spider_list_unicode(self):
        # mybotunicode.egg has two spiders, araña1 and araña2
        self.add_test_version("mybotunicode.egg", "mybotunicode", "r1")
        spiders = get_spider_list("mybotunicode", pythonpath=get_pythonpath_scrapyd())

        self.assertEqual(sorted(spiders), ["araña1", "araña2"])

    def test_failed_spider_list(self):
        self.add_test_version("mybot3.egg", "mybot3", "r1")
        pypath = get_pythonpath_scrapyd()
        # Workaround missing support for context manager in twisted < 15

        # Add -W ignore to sub-python to prevent warnings & tb mixup in stderr
        def popen_wrapper(*args, **kwargs):
            cmd, args = args[0], args[1:]
            cmd = [cmd[0], "-W", "ignore"] + cmd[1:]
            return Popen(cmd, *args, **kwargs)

        with mock.patch("scrapyd.webservice.Popen", wraps=popen_wrapper):
            exc = self.assertRaises(RunnerError, get_spider_list, "mybot3", pythonpath=pypath)
        self.assertRegex(str(exc).rstrip(), r"Exception: This should break the `scrapy list` command$")


class TestWebservice:
    def add_test_version(self, root, basename, version):
        egg_path = Path(__file__).absolute().parent / f"{basename}.egg"
        with open(egg_path, "rb") as f:
            root.eggstorage.put(f, "myproject", version)

    def test_list_spiders(self, txrequest, site_no_egg):
        self.add_test_version(site_no_egg, "mybot", "r1")
        self.add_test_version(site_no_egg, "mybot2", "r2")

        txrequest.args = {b"project": [b"myproject"]}
        endpoint = b"listspiders.json"
        content = site_no_egg.children[endpoint].render_GET(txrequest)

        assert content["spiders"] == ["spider1", "spider2", "spider3"]
        assert content["status"] == "ok"

    def test_list_spiders_nonexistent(self, txrequest, site_no_egg):
        txrequest.args = {
            b"project": [b"nonexistent"],
        }
        endpoint = b"listspiders.json"

        with pytest.raises(error.Error) as exc:
            site_no_egg.children[endpoint].render_GET(txrequest)

        assert exc.value.status == b"200"
        assert exc.value.message == b"project 'nonexistent' not found"

    def test_list_spiders_version(self, txrequest, site_no_egg):
        self.add_test_version(site_no_egg, "mybot", "r1")
        self.add_test_version(site_no_egg, "mybot2", "r2")

        txrequest.args = {
            b"project": [b"myproject"],
            b"_version": [b"r1"],
        }
        endpoint = b"listspiders.json"
        content = site_no_egg.children[endpoint].render_GET(txrequest)

        assert content["spiders"] == ["spider1", "spider2"]
        assert content["status"] == "ok"

    def test_list_spiders_version_nonexistent(self, txrequest, site_no_egg):
        self.add_test_version(site_no_egg, "mybot", "r1")
        self.add_test_version(site_no_egg, "mybot2", "r2")

        txrequest.args = {
            b"project": [b"myproject"],
            b"_version": [b"nonexistent"],
        }
        endpoint = b"listspiders.json"

        with pytest.raises(error.Error) as exc:
            site_no_egg.children[endpoint].render_GET(txrequest)

        assert exc.value.status == b"200"
        assert exc.value.message == b"version 'nonexistent' not found"

    def test_list_versions(self, txrequest, site_with_egg):
        txrequest.args = {
            b"project": [b"quotesbot"],
        }
        endpoint = b"listversions.json"
        content = site_with_egg.children[endpoint].render_GET(txrequest)

        assert content["versions"] == ["0_1"]
        assert content["status"] == "ok"

    def test_list_versions_nonexistent(self, txrequest, site_no_egg):
        txrequest.args = {
            b"project": [b"quotesbot"],
        }
        endpoint = b"listversions.json"
        content = site_no_egg.children[endpoint].render_GET(txrequest)

        assert content["versions"] == []
        assert content["status"] == "ok"

    def test_list_projects(self, txrequest, site_with_egg):
        txrequest.args = {b"project": [b"quotesbot"], b"spider": [b"toscrape-css"]}
        endpoint = b"listprojects.json"
        content = site_with_egg.children[endpoint].render_GET(txrequest)

        assert content["projects"] == ["quotesbot"]

    def test_list_jobs(self, txrequest, site_with_egg):
        txrequest.args = {}
        endpoint = b"listjobs.json"
        content = site_with_egg.children[endpoint].render_GET(txrequest)

        assert set(content) == {"node_name", "status", "pending", "running", "finished"}

    @mock.patch("scrapyd.jobstorage.MemoryJobStorage.__iter__", new=fake_list_jobs)
    def test_list_jobs_finished(self, txrequest, site_with_egg):
        txrequest.args = {}
        endpoint = b"listjobs.json"
        content = site_with_egg.children[endpoint].render_GET(txrequest)

        assert set(content["finished"][0]) == {
            "project",
            "spider",
            "id",
            "start_time",
            "end_time",
            "log_url",
            "items_url",
        }

    def test_delete_version(self, txrequest, site_with_egg):
        endpoint = b"delversion.json"
        txrequest.args = {b"project": [b"quotesbot"], b"version": [b"0.1"]}

        storage = site_with_egg.app.getComponent(IEggStorage)
        version, egg = storage.get("quotesbot")
        if egg:
            egg.close()

        content = site_with_egg.children[endpoint].render_POST(txrequest)
        no_version, no_egg = storage.get("quotesbot")
        if no_egg:
            no_egg.close()

        assert version is not None
        assert content["status"] == "ok"
        assert "node_name" in content
        assert no_version is None

    def test_delete_version_nonexistent_project(self, txrequest, site_with_egg):
        endpoint = b"delversion.json"
        txrequest.args = {b"project": [b"quotesbot"], b"version": [b"nonexistent"]}

        with pytest.raises(error.Error) as exc:
            site_with_egg.children[endpoint].render_POST(txrequest)

        assert exc.value.status == b"200"
        assert exc.value.message == b"version 'nonexistent' not found"

    def test_delete_version_nonexistent_version(self, txrequest, site_no_egg):
        endpoint = b"delversion.json"
        txrequest.args = {b"project": [b"nonexistent"], b"version": [b"0.1"]}

        with pytest.raises(error.Error) as exc:
            site_no_egg.children[endpoint].render_POST(txrequest)

        assert exc.value.status == b"200"
        assert exc.value.message == b"version '0.1' not found"

    def test_delete_project(self, txrequest, site_with_egg):
        endpoint = b"delproject.json"
        txrequest.args = {
            b"project": [b"quotesbot"],
        }

        storage = site_with_egg.app.getComponent(IEggStorage)
        version, egg = storage.get("quotesbot")
        if egg:
            egg.close()

        content = site_with_egg.children[endpoint].render_POST(txrequest)
        no_version, no_egg = storage.get("quotesbot")
        if no_egg:
            no_egg.close()

        assert version is not None
        assert content["status"] == "ok"
        assert "node_name" in content
        assert no_version is None

    def test_delete_project_nonexistent(self, txrequest, site_no_egg):
        endpoint = b"delproject.json"
        txrequest.args = {
            b"project": [b"nonexistent"],
        }

        with pytest.raises(error.Error) as exc:
            site_no_egg.children[endpoint].render_POST(txrequest)

        assert exc.value.status == b"200"
        assert exc.value.message == b"project 'nonexistent' not found"

    def test_addversion(self, txrequest, site_no_egg):
        endpoint = b"addversion.json"
        txrequest.args = {b"project": [b"quotesbot"], b"version": [b"0.1"]}
        egg_path = Path(__file__).absolute().parent / "quotesbot.egg"
        with open(egg_path, "rb") as f:
            txrequest.args[b"egg"] = [f.read()]

        storage = site_no_egg.app.getComponent(IEggStorage)
        version, egg = storage.get("quotesbot")
        if egg:
            egg.close()

        content = site_no_egg.children[endpoint].render_POST(txrequest)
        no_version, no_egg = storage.get("quotesbot")
        if no_egg:
            no_egg.close()

        assert version is None
        assert content["status"] == "ok"
        assert "node_name" in content
        assert no_version == "0_1"

    def test_schedule(self, txrequest, site_with_egg):
        endpoint = b"schedule.json"
        txrequest.args = {b"project": [b"quotesbot"], b"spider": [b"toscrape-css"]}

        content = site_with_egg.children[endpoint].render_POST(txrequest)

        assert site_with_egg.scheduler.calls == [["quotesbot", "toscrape-css"]]
        assert content["status"] == "ok"
        assert "jobid" in content

    def test_schedule_nonexistent_project(self, txrequest, site_no_egg):
        endpoint = b"schedule.json"
        txrequest.args = {b"project": [b"nonexistent"], b"spider": [b"toscrape-css"]}

        with pytest.raises(error.Error) as exc:
            site_no_egg.children[endpoint].render_POST(txrequest)

        assert exc.value.status == b"200"
        assert exc.value.message == b"project 'nonexistent' not found"

    def test_schedule_nonexistent_version(self, txrequest, site_with_egg):
        endpoint = b"schedule.json"
        txrequest.args = {b"project": [b"quotesbot"], b"_version": [b"nonexistent"], b"spider": [b"toscrape-css"]}

        with pytest.raises(error.Error) as exc:
            site_with_egg.children[endpoint].render_POST(txrequest)

        assert exc.value.status == b"200"
        assert exc.value.message == b"version 'nonexistent' not found"

    def test_schedule_nonexistent_spider(self, txrequest, site_with_egg):
        endpoint = b"schedule.json"
        txrequest.args = {b"project": [b"quotesbot"], b"spider": [b"nonexistent"]}

        with pytest.raises(error.Error) as exc:
            site_with_egg.children[endpoint].render_POST(txrequest)

        assert exc.value.status == b"200"
        assert exc.value.message == b"spider 'nonexistent' not found"

    @pytest.mark.parametrize(
        ("endpoint", "attach_egg", "method"),
        [
            (b"addversion.json", True, "render_POST"),
            (b"listversions.json", False, "render_GET"),
            (b"delproject.json", False, "render_POST"),
            (b"delversion.json", False, "render_POST"),
        ],
    )
    def test_project_directory_traversal(self, txrequest, site_no_egg, endpoint, attach_egg, method):
        txrequest.args = {
            b"project": [b"../p"],
            b"version": [b"0.1"],
        }

        if attach_egg:
            egg_path = Path(__file__).absolute().parent / "quotesbot.egg"
            with open(egg_path, "rb") as f:
                txrequest.args[b"egg"] = [f.read()]

        with pytest.raises(DirectoryTraversalError) as exc:
            getattr(site_no_egg.children[endpoint], method)(txrequest)

        assert str(exc.value) == "../p"

        storage = site_no_egg.app.getComponent(IEggStorage)
        version, egg = storage.get("quotesbot")
        if egg:
            egg.close()

        assert version is None

    @pytest.mark.parametrize(
        ("endpoint", "attach_egg", "method"),
        [
            (b"schedule.json", False, "render_POST"),
            (b"listspiders.json", False, "render_GET"),
        ],
    )
    def test_project_directory_traversal_runner(self, txrequest, site_no_egg, endpoint, attach_egg, method):
        txrequest.args = {
            b"project": [b"../p"],
            b"spider": [b"s"],
        }

        if attach_egg:
            egg_path = Path(__file__).absolute().parent / "quotesbot.egg"
            with open(egg_path, "rb") as f:
                txrequest.args[b"egg"] = [f.read()]

        with pytest.raises(DirectoryTraversalError) as exc:
            getattr(site_no_egg.children[endpoint], method)(txrequest)

        assert str(exc.value) == "../p"

        storage = site_no_egg.app.getComponent(IEggStorage)
        version, egg = storage.get("quotesbot")
        if egg:
            egg.close()

        assert version is None
