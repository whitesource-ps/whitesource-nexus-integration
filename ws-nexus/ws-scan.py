#!/usr/bin/python3
import base64
import glob
import json
import logging
import pathlib
import shutil
import sys
import subprocess
import os
from configparser import ConfigParser
from multiprocessing import Pool, Manager
import requests
import re

# constants
BASIC_AUTH_DELIMITER = ':'
PACKAGE_NAME = 'wss-4-nexus'
PACKAGE_VERSION = '1.2.0'
LOG_DIR = 'logs'
SCAN_DIR = '_wstemp'
LOG_FILE_WITH_PATH = LOG_DIR + '/wss-scan.log'
UA_NAME = 'wss-unified-agent'
UA_JAR_NAME = UA_NAME + '.jar'
UA_CONFIG_NAME = UA_NAME + '.config'
WS_LOG_DIR = 'whitesource'
UA_DIR = 'ua'
URL_UA_JAR = 'https://unified-agent.s3.amazonaws.com/wss-unified-agent.jar'
URL_UA_CONFIG = "https://unified-agent.s3.amazonaws.com/wss-unified-agent.config"
ERROR = 'error'
SUCCESS = 'SUCCESS'
FAILED = 'FAILED'
JAR_EXTENSION = 'jar'
SAAS_URL = 'https://saas.whitesourcesoftware.com/agent'
SAAS_EU_URL = 'https://saas-eu.whitesourcesoftware.com/agent'
APP_URL = 'https://app.whitesourcesoftware.com/agent'
APP_EU_URL = 'https://app-eu.whitesourcesoftware.com/agent'
SUPPORTED_FORMATS = {'maven2', 'npm', 'pypi', 'rubygems', 'nuget', 'raw', 'docker'}
DOCKER_TIMEOUT = 600
BETA_REPOS_URL = "/service/rest/beta/repositories"

config = None

logging.basicConfig(level=logging.INFO,
                    handlers=[logging.StreamHandler(stream=sys.stdout), logging.FileHandler(LOG_FILE_WITH_PATH)],
                    format='%(levelname)s %(asctime)s %(process)d: %(message)s',
                    datefmt='%y-%m-%d %H:%M:%S')


class Configuration:
    def __init__(self):
        conf = ConfigParser()
        conf.optionxform = str
        conf.read('../config/params.config')
        # Nexus Settings
        self.nexus_base_url = conf.get('Nexus Settings', 'NexusBaseUrl', fallback='http://localhost:8081').strip('/')
        self.nexus_auth_token = conf.get('Nexus Settings', 'NexusAuthToken')
        self.nexus_user = conf.get('Nexus Settings', 'NexusUser')
        self.nexus_password = conf.get('Nexus Settings', 'NexusPassword')
        self.nexus_config_input_repositories = conf.get('Nexus Settings', 'NexusRepositories')
        # WhiteSource Settings
        self.user_key = conf.get('WhiteSource Settings', 'WSUserKey')
        self.api_key = conf.get('WhiteSource Settings', 'WSApiKey')
        self.product_name = conf.get('WhiteSource Settings', 'WSProductName', fallback='Nexus')
        # self.ua_dir = conf.get('WhiteSource Settings', 'UADir')
        self.check_policies = conf.getboolean('WhiteSource Settings', 'WSCheckPolicies', fallback=False)
        self.policies = 'true' if self.check_policies else 'false'
        self.ws_url = conf.get('WhiteSource Settings', 'WSUrl')
        if not self.ws_url.endswith('/agent'):
            self.ws_url = self.ws_url + '/agent'
        # General Settings
        self.interactive_mode = conf.getboolean('General Settings', 'InteractiveMode', fallback=False)
        self.threads_number = conf.getint('General Settings', 'ThreadCount', fallback=5)

        self.ws_env_var = {**os.environ, **{'WS_USERKEY': self.user_key,
                                            'WS_APIKEY': self.api_key,
                                            'WS_PROJECTPERFOLDER': 'true',
                                            'WS_PRODUCTNAME': self.product_name,
                                            'WS_WSS_URL': self.ws_url,
                                            'WS_INCLUDES': '**/*.*',
                                            'WS_CHECKPOLICIES': self.policies,
                                            'WS_FORCECHECKALLDEPENDENCIES': self.policies,
                                            'WS_OFFLINE': 'false'}}
        self.nexus_ip = self.nexus_base_url.split('//')[1].split(':')[0]


def main():
    global config
    print_header('WhiteSource for Nexus')

    config = Configuration()

    logging.info("Starting")

    nexus_api_url_repos, nexus_api_url_components = define_nexus_parameters()

    validate_nexus_user_pass(config.nexus_user, config.nexus_password, config.nexus_auth_token)

    validate_ws_credentials(config.user_key, config.api_key, config.ws_url)

    config.ua_jar_with_path = download_unified_agent_and_config()

    header, existing_nexus_repository_list = retrieve_nexus_repositories(config.nexus_user, config.nexus_password,
                                                                         config.nexus_auth_token, nexus_api_url_repos)

    if not config.interactive_mode:
        nexus_input_repositories = config.nexus_config_input_repositories
        if not nexus_input_repositories:
            selected_repositories = existing_nexus_repository_list
            logging.info('No repositories specified, all repositories will be scanned')
        else:
            logging.info('Validate specified repositories')
            selected_repositories = validate_selected_repositories_from_config(nexus_input_repositories,
                                                                               existing_nexus_repository_list)
    else:
        print_header('Available Repositories')
        print('Only supported repositories will be available for the WS scan')

        for number, entry in enumerate(existing_nexus_repository_list):
            print(f'   {number} - {entry}')

        nexus_input_repositories_str = input('Select repositories to scan by entering their numbers '
                                             '(space delimited list): ')
        # ToDo - Validate this input - only allow values in range of len(existing_nexus_repository_map)
        nexus_user_input_repositories = nexus_input_repositories_str.split()

        selected_repositories = validate_selected_repositories(nexus_user_input_repositories,
                                                               existing_nexus_repository_list)

    download_components_from_repositories(selected_repositories, nexus_api_url_components, header,
                                          config.threads_number)

    print_header('WhiteSource Scan')
    exit_code = whitesource_scan()

    move_all_files_in_dir(WS_LOG_DIR, LOG_DIR)

    delete_files(WS_LOG_DIR, SCAN_DIR)

    sys.exit(exit_code)


def print_header(hdr_txt: str):
    hdr_txt = ' {0} '.format(hdr_txt)
    hdr = '\n{0}\n{1}\n{0}'.format(('=' * len(hdr_txt)), hdr_txt)
    print(hdr)


def creating_folder_and_log_file():
    """
    Create empty directories for logs and scan results
    Directories from previous runs are deleted

    :return:
    """
    if os.path.exists(LOG_DIR):
        shutil.rmtree(LOG_DIR)
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(SCAN_DIR):
        shutil.rmtree(SCAN_DIR)
    os.makedirs(SCAN_DIR, exist_ok=True)


def define_nexus_parameters():
    global config
    """
    Build Nexus URLs according to configuration

    :return: URLs for repositories and components endpoints
    """
    logging.info('Getting region parameters')
    nexus_api_url = config.nexus_base_url + '/service/rest/v1'
    nexus_api_url_repos = nexus_api_url + '/repositories'
    nexus_api_url_components = nexus_api_url + '/components'

    return nexus_api_url_repos, nexus_api_url_components


def validate_nexus_user_pass(nexus_user, nexus_password, nexus_auth_token):
    """
    :param nexus_user:
    :param nexus_password:
    :param nexus_auth_token:
    :return:
    """
    logging.info('Validating Nexus credentials')

    if (not nexus_auth_token) and (not nexus_user or not nexus_password):
        logging.error(f'{FAILED} {BASIC_AUTH_DELIMITER} Either NexusAuthToken or both NexusUser and NexusPassword must '
                      f'be provided. Check params.config and try again.')
        ws_exit()

    logging.info('Nexus credentials validated')


def validate_ws_credentials(user_key, api_key, ua_url):
    """

    :param user_key:
    :param api_key:
    :param ua_url:
    :return:
    """
    logging.info('Validating WhiteSource User Key, API Key and URL')

    check_if_param_exists(user_key, 'WSUserKey')
    check_if_param_exists(api_key, 'WSApiKey')
    check_if_param_exists(ua_url, 'WSUrl')

    logging.info('WhiteSource User Key, API Key and URL validated')


def check_if_param_exists(param=str, param_name=str):
    if not param:
        logging.error(f'{FAILED} {BASIC_AUTH_DELIMITER} {param_name} '
                      f'must be provided. Check params.config and try again.')
        ws_exit()


def convert_to_basic_string(user_name, password):
    """
    Encode username and password per RFC 7617

    :param user_name:
    :param password:
    :return:
    """
    auth_string_plain = user_name + BASIC_AUTH_DELIMITER + password
    basic_bytes = base64.b64encode(bytes(auth_string_plain, "utf-8"))
    basic_string = str(basic_bytes)[2:-1]
    return basic_string


def retrieve_nexus_repositories(user, password, nexus_auth_token, nexus_api_url_repos):
    """
    Retrieves the list of repositories from Nexus

    :param user:
    :param password:
    :param nexus_auth_token:
    :param nexus_api_url_repos:
    :return:
    """
    if user and password:
        logging.info('Converting user and password to basic string')
        nexus_auth_token = convert_to_basic_string(user, password)
    else:
        nexus_auth_token = nexus_auth_token
    headers = {'Authorization': 'Basic %s' % nexus_auth_token}
    logging.info('Sending request for retrieving Nexus repository list')
    try:
        response_repository_headers = requests.get(nexus_api_url_repos, headers=headers)
        json_response_repository_headers = json.loads(response_repository_headers.text)
    except Exception:
        logging.info(f'{FAILED} to retrieve Nexus repositories. Verify Nexus URL and credentials and try again.')
        ws_exit()

    existing_nexus_repository_list = []
    for json_repository in json_response_repository_headers:
        repo_format = json_repository.get("format")
        if repo_format in SUPPORTED_FORMATS:
            rep_name = json_repository["name"]
            existing_nexus_repository_list.append(rep_name)
        else:
            continue
    return headers, existing_nexus_repository_list


def validate_selected_repositories(nexus_input_repositories, existing_nexus_repository_list):
    """
    Validate selected repositories when running in configMode=False, mostly for testing

    :param nexus_input_repositories:
    :param existing_nexus_repository_list:
    :return:
    """
    try:
        selected_repositories = [existing_nexus_repository_list[int(n)] for n in nexus_input_repositories]
    except Exception:
        # ToDo - After adding input validation to nexus_user_input_repositories (under main() function),
        #        this validation can be removed
        logging.error(f'{FAILED} {BASIC_AUTH_DELIMITER} There are no such repositories in your Nexus environment,'
                      ' please select the number from the list of the existing repositories')
        ws_exit()

    logging.info('Getting region parameters has finished')
    return selected_repositories


def validate_selected_repositories_from_config(nexus_input_repositories, existing_nexus_repository_list):
    """
    Validate selected repositories when running in configMode=True (production mode)

    :param nexus_input_repositories:
    :param existing_nexus_repository_list:
    :return:
    """
    existing_nexus_repository_set = set(existing_nexus_repository_list)
    user_selected_repos_list = list(nexus_input_repositories.split(","))
    user_selected_repos_set = set(user_selected_repos_list)
    missing_repos = user_selected_repos_set - existing_nexus_repository_set
    if missing_repos:
        logging.error(f'Could not find the following repositories: {",".join(missing_repos)}')
        logging.error(f'{FAILED} {BASIC_AUTH_DELIMITER} Specified repositories not found or their format is not'
                      f' supported, check params.config and try again.')
        ws_exit()
    # ToDo - only ws_exit if ALL specified repos not found, continue scan if some were found.

    logging.info('Getting region parameters has finished')
    return user_selected_repos_list


def download_components_from_repositories(selected_repositories, nexus_api_url_components, header, threads_number):
    """
    Download all components from selected repositories and save to folder

    :param selected_repositories:
    :param nexus_api_url_components:
    :param header:
    :param threads_number:
    :return:
    """
    for repo_name in selected_repositories:
        logging.info(f'Repository: {repo_name}')

        repo_comp_url = f'{nexus_api_url_components}?repository={repo_name}'
        continuation_token = "init"
        all_repo_items = []

        logging.info('Validate artifact list')
        while continuation_token:
            if continuation_token != 'init':
                cur_repo_comp_url = f'{repo_comp_url}&continuationToken={continuation_token}'
            else:
                cur_repo_comp_url = repo_comp_url
            cur_response_repo = requests.get(cur_repo_comp_url, headers=header)
            cur_json_response_cur_components = json.loads(cur_response_repo.text)
            for item in cur_json_response_cur_components['items']:
                all_repo_items.append(item)
            continuation_token = cur_json_response_cur_components['continuationToken']

        if not all_repo_items:
            logging.info(f'No artifacts found in {repo_name}')
            logging.info(' -- > ')
        else:
            script_path = pathlib.Path(__file__).parent.absolute()
            cur_dest_folder = f'{script_path}/{SCAN_DIR}/{repo_name}'
            os.makedirs(cur_dest_folder, exist_ok=True)

            logging.info('Retrieving artifacts...')

            manager = Manager()
            docker_images_q = manager.Queue()
            with Pool(threads_number) as pool:
                pool.starmap(repo_worker, [(comp, repo_name, cur_dest_folder, header, config, docker_images_q)
                                           for i, comp in enumerate(all_repo_items)])
            # Updating UA env vars to include Docker images from Nexus
            docker_images = []
            while not docker_images_q.empty():
                docker_images.append(docker_images_q.get(block=True, timeout=0.05))

            if docker_images:
                logging.info(f"Found {len(docker_images)} Docker Images")
                config.ws_env_var['WS_DOCKER_SCANIMAGES'] = 'True'
                config.ws_env_var['WS_DOCKER_INCLUDES'] = ",".join(docker_images)
            logging.info(' -- > ')


def handle_docker_repo(component: dict, conf, header) -> str:
    def get_repo_as_dict() -> dict:
        repo_resp = requests.get(conf.nexus_base_url + BETA_REPOS_URL, headers=header)
        repos_list = json.loads(repo_resp.text)
        repo_dict = {}
        for repo in repos_list:
            repo_dict[repo['name']] = repo

        return repo_dict

    dl_url = component['assets'][0]["downloadUrl"]
    manifest_resp = requests.get(dl_url, headers=header)
    manifest = json.loads(manifest_resp.text)
    repos = get_repo_as_dict()
    import docker
    client = docker.from_env(timeout=DOCKER_TIMEOUT)
    repo_port = repos[component['repository']]['docker'].get('httpsPort', repos[component['repository']]['docker']['httpPort'])
    image_name = f"{conf.nexus_ip}:{repo_port}/{manifest['name']}:{manifest['tag']}"
    logging.info(f"Pulling Docker image: {image_name}")
    try:
        pull_res = client.images.pull(image_name)
    except docker.errors.APIError:
        logging.exception(f"Unable to pull image: {image_name}")
    image_id = pull_res.id.split(':')[1][0:12]
    logging.debug(f"Image:  Image ID: {image_id}")

    return image_id  # Shorten ID to match docker images IMAGE ID


def repo_worker(comp, repo_name, cur_dest_folder, header, conf, d_images_q):
    """

    :param d_images_q:
    :param conf:
    :param comp:
    :param repo_name:
    :param cur_dest_folder:
    :param header:
    """

    all_components = []
    component_assets = comp['assets']
    logging.debug(f"Handling component ID: {comp['id']} on repository: {comp['repository']} Format: {comp['format']}")
    if comp['format'] == 'nuget':
        comp_name = '{}.{}.nupkg'.format(comp['name'], comp['version'])
        all_components.append(comp_name)
    elif re.match('(maven).*', comp['format']):
        component_assets_size = len(component_assets)
        for asset in range(0, component_assets_size):
            comp_name = component_assets[asset]['path'].rpartition('/')[-1]
            if comp_name.split(".")[-1] == JAR_EXTENSION:
                all_components.append(comp_name)
    elif comp['format'] == 'docker':
        d_images_q.put(handle_docker_repo(comp, conf, header))
    else:
        comp_name = component_assets[0]['path'].rpartition('/')[-1]
        all_components.append(comp_name)

    for comp_name in all_components:
        comp_worker(repo_name, component_assets, cur_dest_folder, header, comp_name)


def comp_worker(repo_name, component_assets, cur_dest_folder, header, comp_name):
    """

    :param repo_name:
    :param component_assets:
    :param cur_dest_folder:
    :param header:
    :param comp_name:
    """
    logging.info(f'Downloading {comp_name} component from {repo_name}')
    comp_download_url = component_assets[0]["downloadUrl"]
    response = requests.get(comp_download_url, headers=header)
    logging.debug(f"Download URL: {comp_download_url}")
    path = os.path.dirname(f'{cur_dest_folder}/{comp_name}')
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
    with open(f'{cur_dest_folder}/{comp_name}', 'wb') as f:
        f.write(response.content)
        logging.info(f'Component {comp_name} has successfully downloaded')


def download_unified_agent_and_config():
    """
    Download unified agent and config file if there are not exist in the default or specified folder
    :return:
    """
    logging.info('Verifying agent parameters')
    ua_jar_with_path = f'{UA_DIR}/{UA_JAR_NAME}'
    ua_conf_with_path = f'{UA_DIR}/{UA_CONFIG_NAME}'

    if not os.path.isdir(UA_DIR):
        logging.info(f'Creating directory "{UA_DIR}"')
        os.makedirs(UA_DIR, exist_ok=True)

    if not os.path.isfile(ua_jar_with_path):
        logging.info('(this may take a few minutes on first run)')
        logging.info('Downloading WhiteSource agent')

        r = requests.get(URL_UA_JAR)
        with open(ua_jar_with_path, 'wb') as f:
            f.write(r.content)

        r = requests.get(URL_UA_CONFIG)
        with open(ua_conf_with_path, 'wb') as f:
            f.write(r.content)

    logging.info('WhiteSource agent download complete')

    return ua_jar_with_path


def whitesource_scan() -> int:
    global config
    """

    :param product_name:
    :param user_key:
    :param api_key:
    :param ws_url:
    :param check_policies:
    :param ua_jar_with_path:
    :return:
    """

    logging.info('Starting WhiteSource scan')

    return_code = subprocess.run(['java', '-jar', config.ua_jar_with_path, '-d', SCAN_DIR, '-logLevel', ERROR],
                                 env=config.ws_env_var, stdout=subprocess.DEVNULL).returncode

    return_msg = 'SUCCESS'
    if return_code != 0:
        return_code = return_code - 4294967296
        if return_code == -1:
            return_msg = 'ERROR'
        elif return_code == -2:
            return_msg = 'POLICY_VIOLATION'
        elif return_code == -3:
            return_msg = 'CLIENT_FAILURE'
        elif return_code == -4:
            return_msg = 'CONNECTION_FAILURE'
        elif return_code == -5:
            return_msg = 'SERVER_FAILURE'
        elif return_code == -6:
            return_msg = 'PRE_STEP_FAILURE'
        else:
            return_msg = 'FAILED'

    logging.info('WhiteSource scan complete')

    logging.info(f'Result: {return_msg} ({return_code})')
    return return_code


def move_all_files_in_dir(src_dir, dst_dir):
    """

    :param src_dir:
    :param dst_dir:
    :return:
    """
    logging.info('Moving logs after the WhiteSource scan has finished')
    if os.path.isdir(src_dir) and os.path.isdir(dst_dir):
        for filePath in glob.glob(src_dir):
            shutil.move(filePath, dst_dir)
            logging.info('Moving logs has successfully finished')
    else:
        logging.info(f'{src_dir} or {dst_dir} are not directories')
        logging.info('Moving logs after WhiteSource scan has failed')


def delete_files(ws_dir, scan_dir):
    """

    :param ws_dir:
    :param scan_dir:
    :return:
    """
    logging.info('Start deleting the scan dir')
    success = True
    if os.path.isdir(ws_dir):
        try:
            os.rmdir(ws_dir)
        except OSError as e:
            logging.error(f'Deleting the scan dir has failed: {ws_dir}: {e.strerror}')
            success = False
    if os.path.isdir(scan_dir):
        try:
            shutil.rmtree(scan_dir)
        except OSError as e:
            logging.error(f'Deleting the scan dir has failed: {scan_dir}: {e.strerror}')
            success = False
    if success:
        logging.info('Deleting the scan dir has successfully finished')
    else:
        sys.exit(1)


def ws_exit():
    delete_files(WS_LOG_DIR, SCAN_DIR)
    sys.exit(1)


if __name__ == '__main__':
    main()
