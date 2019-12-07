""" Implements the commands for preprocessing webpages before training """
from typing import List

from blaze.chrome.devtools import capture_har_in_replay_server
from blaze.config.client import get_default_client_environment
from blaze.config.config import get_config
from blaze.config.environment import EnvironmentConfig, PushGroup
from blaze.logger import logger as log
from blaze.mahimahi.server.filestore import FileStore
from blaze.preprocess.har import har_entries_to_resources
from blaze.preprocess.record import find_url_stable_set, get_page_links as _get_page_links, record_webpage
from blaze.preprocess.resource import resource_list_to_push_groups
from blaze.preprocess.url import Url

from . import command


@command.argument("website", help="The URL of the website to prepare for training")
@command.argument("--record_dir", help="The directory to store the recorded webpage", required=True)
@command.command
def record(args):
    """
    Record a website using Mahimahi. Stores the recorded files in the specified directory. In order
    to use it with blaze, you must preprocess it using `blaze preprocess` to generate a training
    manifest.
    """
    log.info("recording website", website=args.website, record_dir=args.record_dir)

    config = get_config()
    log.debug("using configuration", **config._asdict())
    record_webpage(args.website, args.record_dir, config)


@command.argument("website", help="The URL of the website to prepare for training")
@command.argument("--output", help="The location to save the prepared manifest", required=True)
@command.argument("--record_dir", help="The directory of the recorded webpage", required=True)
@command.argument(
    "--extract_critical_requests",
    help="Returns the response taking into account the critical resources in the page",
    action="store_true",
)
@command.argument(
    "--train_domain_globs",
    nargs="*",
    help="The glob patterns of domain names to enable training for. "
    "By default this will be *.domain of the given URL",
)
@command.command
def preprocess(args):
    """
    Preprocesses a website for training. Automatically discovers linked pages up to a certain depth
    and finds the stable set of page dependencies. The page load is recorded and stored and a
    training manifest is outputted.
    """
    domain = Url.parse(args.website).domain
    train_domain_globs = args.train_domain_globs or ["*{}*".format(domain)]
    log.info(
        "preprocessing website", website=args.website, record_dir=args.record_dir, train_domain_globs=train_domain_globs
    )

    config = get_config(env_config=EnvironmentConfig(replay_dir=args.record_dir, request_url=args.website))
    log.debug("using configuration", **config._asdict())

    log.info("capturing execution")
    client_env = get_default_client_environment()
    har_resources = get_har_resources(args.website, config, client_env, args.extract_critical_requests)

    log.info("finding dependency stable set...")
    res_list = find_url_stable_set(args.website, config)

    log.info("found total dependencies", total=len(res_list))
    push_groups = resource_list_to_push_groups(res_list, train_domain_globs=train_domain_globs)

    log.info("finding cacheable objects")
    push_groups = annotate_cacheable_objects(args.record_dir, push_groups)

    log.info("generating configuration...")
    env_config = EnvironmentConfig(
        replay_dir=args.record_dir, request_url=args.website, push_groups=push_groups, har_resources=har_resources
    )
    env_config.save_file(args.output)
    log.info("successfully prepared website for training", output=args.output)


@command.argument("--from_manifest", help="The manifest file of the recorded webpage", required=True)
@command.command
def print_cache_times(args):
    """ Finds pages links on the given page up to the given depth """
    log.info("reading cache information", website=args.website, max_depth=args.max_depth)
    env_config = EnvironmentConfig.load_file(args.from_manifest)
    annotate_cacheable_objects(env_config.replay_dir, env_config.push_groups)
    for group in env_config.push_groups:
        for res in group.resources:
            print(f"{res.url:<48}  {res.cache_time}")


def get_har_resources(website, config, client_env, extract_critical_requests):
    """
    Returns the HAR entries in the website including information about the critical requests.
    """
    har = capture_har_in_replay_server(website, config, client_env)
    if not extract_critical_requests:
        return har_entries_to_resources(har)
    har2 = capture_har_in_replay_server(website, config, client_env, extract_critical_requests=True)

    critical_requests = set(h.request.url for h in har2.log.entries if h.critical)
    for res in har.log.entries:
        if res.request.url in critical_requests:
            print("doing some critical work")
            res._replace(critical=True)

    return har_entries_to_resources(har)


def annotate_cacheable_objects(record_dir: str, push_groups: List[PushGroup]) -> List[PushGroup]:
    """
    Modifies the passed in push groups by examining files in the record directory
    and annotating the ones that are cacheable
    """
    filestore = FileStore(record_dir)
    cache_times = {
        **{f"http://{f.host}{f.uri}": f.cache_time for f in filestore.cacheable_files},
        **{f"https://{f.host}{f.uri}": f.cache_time for f in filestore.cacheable_files},
    }
    for group in push_groups:
        for res in group.resources:
            cache_time = cache_times.get(res.url, 0)
            if cache_time > 0:
                res._replace(cache_time=cache_time)

    return push_groups
