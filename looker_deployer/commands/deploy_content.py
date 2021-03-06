import os
import re
import subprocess
import logging
import tempfile
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from looker_deployer.utils import deploy_logging
from looker_deployer.utils import parse_ini
from looker_sdk import client, models


logger = deploy_logging.get_logger(__name__)


def get_client(ini, env):
    sdk = client.setup(config_file=ini, section=env)
    return sdk


def get_space_ids_from_name(space_name, parent_id, sdk):
    if (space_name == "Shared" and parent_id == "0"):
        return ["1"]
    space_list = sdk.search_spaces(name=space_name, parent_id=parent_id)
    id_list = [i.id for i in space_list]

    return id_list


def create_or_return_space(space_name, parent_id, sdk):

    try:
        target_id = get_space_ids_from_name(space_name, parent_id, sdk)
        logger.debug("Space ID from name", extra={"id": target_id})
        assert len(target_id) == 1
    except AssertionError as e:
        if len(target_id) > 1:
            logger.error("More than one Space found with that parent/name", extra={"space_ids": target_id})
            raise e
        else:
            logger.warning("No folders found. Creating folder now")
            new_space = models.Space(name=space_name, parent_id=parent_id)
            res = sdk.create_space(new_space)
            return res.id

    logger.info("Found Space ID", extra={"id": target_id})
    return target_id[0]


def get_gzr_creds(ini, env):
    ini = parse_ini.read_ini(ini)
    env_record = ini[env]
    host = env_record["base_url"].lstrip("https://").split(":")[0]
    client_id = env_record["client_id"]
    client_secret = env_record["client_secret"]
    verify_ssl = env_record["verify_ssl"]

    return (host, client_id, client_secret, verify_ssl)


def export_spaces(env, ini, path):
    host, client_id, client_secret, verify_ssl = get_gzr_creds(ini, env)

    gzr_command = [
        "gzr",
        "space",
        "export",
        "1",
        "--dir",
        path,
        "--host",
        host,
        "--client-id",
        client_id,
        "--client-secret",
        client_secret
    ]

    # config parser returns a string - easier to parse that than convert to a bool
    if verify_ssl == "False":
        gzr_command.append("--no-verify-ssl")

    subprocess.run(gzr_command)


def import_content(content_type, content_json, space_id, env, ini):
    assert content_type in ["dashboard", "look"], "Unsupported Content Type"
    host, client_id, client_secret, verify_ssl = get_gzr_creds(ini, env)

    logger.info(
        "Deploying content",
        extra={
            "content_type": content_type,
            "source_file": content_json,
            "folder_id": space_id,
            "host": host,
            "verify_ssl": verify_ssl,
            "active_thread": threading.get_ident()
        }
    )

    gzr_command = [
        "gzr",
        content_type,
        "import",
        content_json,
        space_id,
        "--host",
        host,
        "--client-id",
        client_id,
        "--client-secret",
        client_secret,
        "--force"
    ]

    # config parser returns a string - easier to parse that than convert to a bool
    if verify_ssl == "False":
        gzr_command.append("--no-verify-ssl")

    subprocess.run(gzr_command)


def build_spaces(spaces, sdk):
    # seeding initial value of parent id to Shared
    # We use a list to aid in debugging should values not drain properly"
    id_tracker = ["0"]

    for space in spaces:
        logger.debug("parent_id to use", extra={"id_tracker": id_tracker})
        # Pull last value from id_tracker
        space_parent = id_tracker.pop()

        logger.debug("data for folder creation", extra={"folder": space, "folder_parent": space_parent})
        space_id = create_or_return_space(space, space_parent, sdk)

        # Add new id to id_tracker
        id_tracker.append(space_id)
        logger.debug("parent_id_tracker updated", extra={"parent_id_tracker": id_tracker})

    # We need the final value of the id_tracker so we know what id to deploy content to
    return id_tracker[0]


def deploy_space(s, sdk, env, ini, recursive):

    logger.debug("working folder", extra={"working_folder": s})

    # grab the relevant files for deployment
    space_files = [f for f in os.listdir(s) if os.path.isfile(os.path.join(s, f))]
    space_children = [os.path.join(s, d) + os.sep for d in os.listdir(s) if os.path.isdir(os.path.join(s, d))]
    look_files = [os.path.join(s, i) for i in space_files if re.search("^Look", i)]
    dash_files = [os.path.join(s, i) for i in space_files if re.search("^Dashboard", i)]
    logger.debug("files to process", extra={"looks": look_files, "dashboards": dash_files})

    # cut down directory to looker-specific paths
    a, b, c = s.partition("Shared")  # Hard coded to Shared for now TODO: Change this!
    c = c.rpartition(os.sep)[0]
    logger.debug("partition components", extra={"a": a, "b": b, "c": c})

    # turn into a list of spaces to process
    spaces_to_process = "".join([b, c]).split(os.sep)
    logger.debug("folders to process", extra={"folders": spaces_to_process})

    # The final value of id_tracker in build_spaces must be the targeted space id
    space_id = build_spaces(spaces_to_process, sdk)
    logger.debug("target folder id", extra={"folder_id": space_id})

    # deploy looks
    logger.debug("running looks", extra={"looks": look_files})
    with ThreadPoolExecutor(max_workers=3) as pool:
        pool.map(
            import_content,
            repeat("look"),
            look_files,
            repeat(space_id),
            repeat(env),
            repeat(ini)
        )
    # deploy dashboards
    logger.debug("running dashboards", extra={"dashboards": dash_files})
    with ThreadPoolExecutor(max_workers=3) as pool:
        pool.map(
            import_content,
            repeat("dashboard"),
            dash_files,
            repeat(space_id),
            repeat(env),
            repeat(ini)
        )

    # go for recursion
    if recursive and space_children:
        logger.info("Attemting Recursion of children folders", extra={"children_folders": space_children})
        for child in space_children:
            deploy_space(child, sdk, env, ini, recursive)
    else:
        logger.info("No Recursion specified or empty child list", extra={"children_folders": space_children})


def deploy_content(content_type, content, sdk, env, ini):
    # extract directory path
    dirs = content.rpartition(os.sep)[0] + os.sep

    # cut down directory to looker-specific paths
    a, b, c = dirs.partition("Shared")  # Hard coded to Shared for now TODO: Change this!
    c = c.rpartition(os.sep)[0]  # strip trailing slash

    # turn into a list of spaces to process
    spaces_to_process = "".join([b, c]).split(os.sep)

    # The final value of id_tracker in build_spaces must be the targeted space id
    space_id = build_spaces(spaces_to_process, sdk)

    import_content(content_type, content, space_id, env, ini)


def send_content(
    sdk, env, ini, target_folder=None, spaces=None, dashboards=None, looks=None, recursive=False
):

    if spaces:
        logger.debug("Deploying folders", extra={"folders": spaces})
        # Loop through spaces
        for s in spaces:
            logger.debug("working folder", extra={"working_folder": s})
            # Check for a target space override
            if target_folder:
                logger.info("target folder override found", extra={"target_folder": target_folder})
                # In order for recursion to continue to work properly, the actual directory needs to be updated
                # Create a temporary directory to contain updated space. Context block will auto-clean when done
                with tempfile.TemporaryDirectory() as d:
                    updated_space = os.path.join(d, target_folder)
                    # copy the source space directory tree to target space override
                    shutil.copytree(s, updated_space)
                    # kick off the job from the new space
                    deploy_space(updated_space, sdk, env, ini, recursive)
            # If no target space override, kick off job normally
            else:
                deploy_space(s, sdk, env, ini, recursive)
    if dashboards:
        logger.debug("Deploying dashboards", extra={"dashboards": dashboards})
        for dash in dashboards:
            logger.debug("working dashboard", extra={"dashboard": dash})
            # Check for target space override
            if target_folder:
                logger.info("target folder override found", extra={"target_folder": target_folder})
                # In order for recursion to continue to work properly, the actual directory needs to be updated
                # Create a temporary directory to contain updated space. Context block will auto-clean when done
                with tempfile.TemporaryDirectory() as d:
                    # copy the dashboard file to target space override
                    target_dir = os.path.join(d, target_folder)
                    os.makedirs(target_dir)
                    shutil.copy(dash, target_dir)
                    new_dash_path = [os.path.join(target_dir, f) for f in os.listdir(target_dir)][0]
                    # kick off the job from the new space
                    deploy_content("dashboard", new_dash_path, sdk, env, ini)
            else:
                deploy_content("dashboard", dash, sdk, env, ini)
    if looks:
        logger.debug("Deploying looks", extra={"looks": looks})
        for look in looks:
            logger.debug("working look", extra={"look": look})
            # Check for target space override
            if target_folder:
                logger.info("target folder override found", extra={"target_folder": target_folder})
                # In order for recursion to continue to work properly, the actual directory needs to be updated
                # Create a temporary directory to contain updated space. Context block will auto-clean when done
                with tempfile.TemporaryDirectory() as d:
                    # copy the look file to target space override
                    target_dir = os.path.join(d, target_folder)
                    os.makedirs(target_dir)
                    shutil.copy(look, target_dir)
                    new_look_path = [os.path.join(target_dir, f) for f in os.listdir(target_dir)][0]
                    # kick off the job from the new space
                    deploy_content("look", new_look_path, sdk, env, ini)
            else:
                deploy_content("look", look, sdk, env, ini)


def main(args):

    if args.debug:
        logger.setLevel(logging.DEBUG)

    logger.debug("ini file", extra={"ini": args.ini})

    if args.target_folder:
        # Force any target space override to start from Shared
        assert args.target_folder.startswith("Shared"), "Target Space MUST begin with 'Shared'"
        # Make sure trailing sep is in place
        if not args.target_folder.endswith(os.sep):
            args.target_folder += os.sep

    if args.export:
        logger.info("Pulling content from dev", extra={"env": args.env, "pull_location": args.export})
        export_spaces(args.env, args.ini, args.export)
    else:
        sdk = get_client(args.ini, args.env)
        send_content(
            sdk,
            args.env,
            args.ini,
            args.target_folder,
            args.folders,
            args.dashboards,
            args.looks,
            args.recursive
        )
