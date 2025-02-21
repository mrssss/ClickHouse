#!/usr/bin/env python3

import argparse

import atexit
import logging
import sys
import subprocess
from pathlib import Path

from typing import Dict

from github import Github

from build_download_helper import download_builds_filter
from clickhouse_helper import (
    ClickHouseHelper,
    mark_flaky_tests,
    prepare_tests_results_for_clickhouse,
)
from commit_status_helper import post_commit_status, update_mergeable_check
from docker_pull_helper import get_image_with_version, DockerImage
from env_helper import CI, TEMP_PATH as TEMP, REPORTS_PATH
from get_robot_token import get_best_robot_token
from pr_info import PRInfo
from report import TestResults, TestResult
from rerun_helper import RerunHelper
from s3_helper import S3Helper
from stopwatch import Stopwatch
from tee_popen import TeePopen
from upload_result_helper import upload_results


RPM_IMAGE = "clickhouse/install-rpm-test"
DEB_IMAGE = "clickhouse/install-deb-test"
TEMP_PATH = Path(TEMP)
SUCCESS = "success"
FAILURE = "failure"


def prepare_test_scripts():
    server_test = r"""#!/bin/bash
systemctl start clickhouse-server
clickhouse-client -q 'SELECT version()'"""
    keeper_test = r"""#!/bin/bash
systemctl start clickhouse-keeper
for i in {1..20}; do
    echo wait for clickhouse-keeper to being up
    > /dev/tcp/127.0.0.1/9181 2>/dev/null && break || sleep 1
done
for i in {1..5}; do
    echo wait for clickhouse-keeper to answer on mntr request
    exec 13<>/dev/tcp/127.0.0.1/9181
    echo mntr >&13
    cat <&13 | grep zk_version && break || sleep 1
    exec 13>&-
done
exec 13>&-"""
    binary_test = r"""#!/bin/bash
chmod +x /packages/clickhouse
/packages/clickhouse install
clickhouse-server start --daemon
for i in {1..5}; do
    clickhouse-client -q 'SELECT version()' && break || sleep 1
done
clickhouse-keeper start --daemon
for i in {1..20}; do
    echo wait for clickhouse-keeper to being up
    > /dev/tcp/127.0.0.1/9181 2>/dev/null && break || sleep 1
done
for i in {1..5}; do
    echo wait for clickhouse-keeper to answer on mntr request
    exec 13<>/dev/tcp/127.0.0.1/9181
    echo mntr >&13
    cat <&13 | grep zk_version && break || sleep 1
    exec 13>&-
done
exec 13>&-"""
    (TEMP_PATH / "server_test.sh").write_text(server_test, encoding="utf-8")
    (TEMP_PATH / "keeper_test.sh").write_text(keeper_test, encoding="utf-8")
    (TEMP_PATH / "binary_test.sh").write_text(binary_test, encoding="utf-8")


def test_install_deb(image: DockerImage) -> TestResults:
    tests = {
        "Install server deb": r"""#!/bin/bash -ex
apt-get install /packages/clickhouse-{server,client,common}*deb
bash -ex /packages/server_test.sh""",
        "Install keeper deb": r"""#!/bin/bash -ex
apt-get install /packages/clickhouse-keeper*deb
bash -ex /packages/keeper_test.sh""",
        "Install clickhouse binary in deb": r"bash -ex /packages/binary_test.sh",
    }
    return test_install(image, tests)


def test_install_rpm(image: DockerImage) -> TestResults:
    # FIXME: I couldn't find why Type=notify is broken in centos:8
    # systemd just ignores the watchdog completely
    tests = {
        "Install server rpm": r"""#!/bin/bash -ex
yum localinstall --disablerepo=* -y /packages/clickhouse-{server,client,common}*rpm
echo CLICKHOUSE_WATCHDOG_ENABLE=0 > /etc/default/clickhouse-server
bash -ex /packages/server_test.sh""",
        "Install keeper rpm": r"""#!/bin/bash -ex
yum localinstall --disablerepo=* -y /packages/clickhouse-keeper*rpm
bash -ex /packages/keeper_test.sh""",
        "Install clickhouse binary in rpm": r"bash -ex /packages/binary_test.sh",
    }
    return test_install(image, tests)


def test_install_tgz(image: DockerImage) -> TestResults:
    # FIXME: I couldn't find why Type=notify is broken in centos:8
    # systemd just ignores the watchdog completely
    tests = {
        f"Install server tgz in {image.name}": r"""#!/bin/bash -ex
[ -f /etc/debian_version ] && CONFIGURE=configure || CONFIGURE=
for pkg in /packages/clickhouse-{common,client,server}*tgz; do
    package=${pkg%-*}
    package=${package##*/}
    tar xf "$pkg"
    "/$package/install/doinst.sh" $CONFIGURE
done
[ -f /etc/yum.conf ] && echo CLICKHOUSE_WATCHDOG_ENABLE=0 > /etc/default/clickhouse-server
bash -ex /packages/server_test.sh""",
        f"Install keeper tgz in {image.name}": r"""#!/bin/bash -ex
[ -f /etc/debian_version ] && CONFIGURE=configure || CONFIGURE=
for pkg in /packages/clickhouse-keeper*tgz; do
    package=${pkg%-*}
    package=${package##*/}
    tar xf "$pkg"
    "/$package/install/doinst.sh" $CONFIGURE
done
bash -ex /packages/keeper_test.sh""",
    }
    return test_install(image, tests)


def test_install(image: DockerImage, tests: Dict[str, str]) -> TestResults:
    test_results = []  # type: TestResults
    for name, command in tests.items():
        stopwatch = Stopwatch()
        container_name = name.lower().replace(" ", "_").replace("/", "_")
        log_file = TEMP_PATH / f"{container_name}.log"
        run_command = (
            f"docker run --rm --privileged --detach --cap-add=SYS_PTRACE "
            f"--volume={TEMP_PATH}:/packages {image}"
        )
        logging.info("Running docker container: `%s`", run_command)
        container_id = subprocess.check_output(
            run_command, shell=True, encoding="utf-8"
        ).strip()
        (TEMP_PATH / "install.sh").write_text(command)
        install_command = f"docker exec {container_id} bash -ex /packages/install.sh"
        with TeePopen(install_command, log_file) as process:
            retcode = process.wait()
            if retcode == 0:
                status = SUCCESS
            else:
                status = FAILURE

        subprocess.check_call(f"docker kill -s 9 {container_id}", shell=True)
        test_results.append(
            TestResult(name, status, stopwatch.duration_seconds, [log_file])
        )

    return test_results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="The script to check if the packages are able to install",
    )

    parser.add_argument(
        "check_name",
        help="check name, used to download the packages",
    )
    parser.add_argument("--download", default=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-download",
        dest="download",
        action="store_false",
        default=argparse.SUPPRESS,
        help="if set, the packages won't be downloaded, useful for debug",
    )
    parser.add_argument("--deb", default=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-deb",
        dest="deb",
        action="store_false",
        default=argparse.SUPPRESS,
        help="if set, the deb packages won't be checked",
    )
    parser.add_argument("--rpm", default=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-rpm",
        dest="rpm",
        action="store_false",
        default=argparse.SUPPRESS,
        help="if set, the rpm packages won't be checked",
    )
    parser.add_argument("--tgz", default=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-tgz",
        dest="tgz",
        action="store_false",
        default=argparse.SUPPRESS,
        help="if set, the tgz packages won't be checked",
    )

    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.INFO)

    stopwatch = Stopwatch()

    args = parse_args()

    TEMP_PATH.mkdir(parents=True, exist_ok=True)

    pr_info = PRInfo()

    if CI:
        gh = Github(get_best_robot_token(), per_page=100)
        atexit.register(update_mergeable_check, gh, pr_info, args.check_name)

        rerun_helper = RerunHelper(gh, pr_info, args.check_name)
        if rerun_helper.is_already_finished_by_status():
            logging.info(
                "Check is already finished according to github status, exiting"
            )
            sys.exit(0)

    docker_images = {
        name: get_image_with_version(REPORTS_PATH, name)
        for name in (RPM_IMAGE, DEB_IMAGE)
    }
    prepare_test_scripts()

    if args.download:

        def filter_artifacts(path: str) -> bool:
            return (
                path.endswith(".deb")
                or path.endswith(".rpm")
                or path.endswith(".tgz")
                or path.endswith("/clickhouse")
            )

        download_builds_filter(
            args.check_name, REPORTS_PATH, TEMP_PATH, filter_artifacts
        )

    test_results = []  # type: TestResults
    if args.deb:
        test_results.extend(test_install_deb(docker_images[DEB_IMAGE]))
    if args.rpm:
        test_results.extend(test_install_rpm(docker_images[RPM_IMAGE]))
    if args.tgz:
        test_results.extend(test_install_tgz(docker_images[DEB_IMAGE]))
        test_results.extend(test_install_tgz(docker_images[RPM_IMAGE]))

    state = SUCCESS
    description = "Packages installed successfully"
    if FAILURE in (result.status for result in test_results):
        state = FAILURE
        description = "Failed to install packages: " + ", ".join(
            result.name for result in test_results
        )

    s3_helper = S3Helper()

    report_url = upload_results(
        s3_helper,
        pr_info.number,
        pr_info.sha,
        test_results,
        [],
        args.check_name,
    )
    print(f"::notice ::Report url: {report_url}")
    if not CI:
        return

    ch_helper = ClickHouseHelper()
    mark_flaky_tests(ch_helper, args.check_name, test_results)

    if len(description) >= 140:
        description = description[:136] + "..."

    post_commit_status(gh, pr_info.sha, args.check_name, description, state, report_url)

    prepared_events = prepare_tests_results_for_clickhouse(
        pr_info,
        test_results,
        state,
        stopwatch.duration_seconds,
        stopwatch.start_time_str,
        report_url,
        args.check_name,
    )

    ch_helper.insert_events_into(db="default", table="checks", events=prepared_events)

    if state == FAILURE:
        sys.exit(1)


if __name__ == "__main__":
    main()
