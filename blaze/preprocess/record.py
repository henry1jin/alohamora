""" This module implements utilities to record and pre-process live web page loads """
import collections
import functools
import subprocess
import tempfile
from typing import List, Set
from xml.etree import ElementTree

import requests

from blaze.config import Config
from blaze.config.environment import Resource
from blaze.chrome.config import get_chrome_command, get_chrome_flags
from blaze.chrome.devtools import capture_har
from blaze.logger import logger
from blaze.mahimahi import MahiMahiConfig
from blaze.util.seq import ordered_uniq

from .har import har_entries_to_resources
from .url import Url

STABLE_SET_NUM_RUNS = 10


def record_webpage(url: str, save_dir: str, config: Config):
    """
    Given a URL and runtime configuration, record_webpage creates a Mahimahi record
    shell and records the web page load in Chrome. It saves the result to the given
    save directory, which is expected to be empty. A subprocess.CalledProcessError
    is raised if an error occurs
    """
    with tempfile.TemporaryDirectory(prefix="blaze_record", dir="/tmp") as tmp_dir:
        chrome_flags = get_chrome_flags(tmp_dir)
        chrome_cmd = get_chrome_command(url, chrome_flags, config)

        mm_config = MahiMahiConfig(config)
        cmd = mm_config.record_shell_with_cmd(save_dir, chrome_cmd)

        proc = subprocess.run(" ".join(cmd), shell=True)
        proc.check_returncode()


def find_url_stable_set(url: str, config: Config) -> List[Resource]:
    """
    Loads the given URL `STABLE_SET_NUM_RUNS` times back-to-back and records the HAR file
    generated by chrome. It then finds the common URLs across the page loads, computes their
    relative ordering, and returns a list of PushGroups for the webpage
    """
    log = logger.with_namespace("find_url_stable_set")
    resource_lists: List[Set[Resource]] = []
    pos_dict = collections.defaultdict(lambda: collections.defaultdict(int))
    for n in range(STABLE_SET_NUM_RUNS):
        log.debug("capturing HAR...", run=n + 1, url=url)
        har = capture_har(url, config)
        resource_list = har_entries_to_resources(har.log.entries)
        if not resource_list:
            log.warn("no response received", run=n + 1)
            continue
        log.debug("received resources", total=len(resource_list))

        for i in range(len(resource_list)):  # pylint: disable=consider-using-enumerate
            for j in range(i + 1, len(resource_list)):
                pos_dict[resource_list[i].url][resource_list[j].url] += 1
        resource_lists.append(set(resource_list))

    log.debug("resource list lengths", resource_lens=list(map(len, resource_lists)))
    common_res = list(set.intersection(*resource_lists)) if resource_lists else []
    common_res.sort(key=functools.cmp_to_key(lambda a, b: -pos_dict[a.url][b.url] + (len(resource_lists) // 2)))
    return common_res


def get_page_links(url: str, max_depth: int = 1) -> List[str]:
    """
    Performs DFS with the given max_depth on the given URL to discover all
    <a href="..."> links in the page
    """
    if max_depth == 0:
        return []

    log = logger.with_namespace("get_page_links").with_context(depth_left=max_depth)
    try:
        log.info("fetching page", url=url)
        page = requests.get(url)
        page.raise_for_status()
        page_text = page.text
    except requests.exceptions.RequestException as err:
        log.warn("failed to fetch page", error=repr(err))
        return []

    try:
        log.debug("parsing http response", length=len(page_text))
        root = ElementTree.fromstring(page_text)
    except ElementTree.ParseError as err:
        log.warn("failed to parse response", error=repr(err))
        return []

    parsed_links = root.findall(".//a")
    log.info("found links", url=url, n_links=len(parsed_links))

    links = []
    domain = Url.parse(url).domain
    for link in parsed_links:
        link_url = link.get("href")
        if not any(link_url.startswith(prefix) for prefix in {"http", "/"}):
            log.debug("ignoring found link (bad prefix)", link=link_url)
            continue

        link_domain = Url.parse(link_url).domain
        if link_domain != domain:
            log.debug("ignoring found link (bad domain)", link=link_url)
            continue

        links.append(link_url)
        links.extend(get_page_links(link_url, max_depth - 1))
    return ordered_uniq(links)
