import argparse
import copy
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from hashlib import md5
from sys import exit
from typing import Dict, List, Optional

import requests
import win32api
from errorhandler import ErrorHandler

from __init__ import __version__

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"

INSTALLER_REGEX = (r"(?:\\|/|^)setup((?:_+[A-Za-zÁ-Úá-úÑñ0-9\-.]+)+)(_.+)?(_+\([0-9]+\)|_[0-9]+(?:\.[0-9]+)+)(_\(["
                   r"^\)]+\))?\.exe$")
PRODUCT_ID_REGEX_1 = r"^.+?\"tmp\\([0-9]+)\.ini\""
PRODUCT_ID_REGEX_2 = r"^.+?\"(?:.+?\\)?goggame-([0-9]+)\.(?:hashdb|info|script|id)\""
OLD_VERSION_REGEX = r"_([0-9]+(?:\.[0-9]+)+)\.exe"
EXTRACT_VERSION_REGEX = r"_+((?:v\.?)?(?:[a-zá-úñ0-9]+\-)?(?:[0-9\-]+(?:\.[0-9a-z\-_]+?(?:\([^\)]+?\))?)*))_\("
BUILD_ID_REGEX = r".+?\.\[([0-9]+)\]"
VERSION_NAME_REGEX = r"(.+?)\.(?:\[[0-9]*\]?)?$"

UNKNOWN = "Unknown"
CURRENT_DATE = datetime.today().strftime("%Y%m%d_%H%M%S")

global_exe_info = {}

global STR_TO_REMOVE_FROM_NAME
global DATA_FILE_CONTENT
global REPLACE_NAMES
global MATCH_VERSIONS
global ROMAN_NUMERALS
global GOODIES_ID
global DELISTED_GAMES


# TODO:
#  - Store API responses to use with get_local_version_from_gog() instead of re-downloading it.
#  - Maybe add a function to retrieve account games for delisted games, mainly for old gen installers as we rely on
#    public search to get their product ID.


def main():
    parser = argparse.ArgumentParser(prog="gog-installer-update-checker",
                                     description="Check GOG installer for updates")
    parser.add_argument("-v", "--version",
                        action="version",
                        version=f"%(prog)s v{__version__}")
    parser.add_argument("--path",
                        help="Path(s) to directories containing GOG installers.",
                        nargs="*",
                        required=True,
                        action="extend")
    parser.add_argument("--innoextract-path",
                        help="Path to the innoextract executable. By default taken from PATH.",
                        nargs="?",
                        default=shutil.which("innoextract"))
    parser.add_argument("--output-file",
                        help="Path to the file where the installers with found updates will be listed. Current date is "
                             "appended to the name. Default is no output file.",
                        nargs="?",
                        default=None)
    parser.add_argument("--log-level",
                        help="How much stuff is logged. Can be 'debug', 'info', 'warning', 'error'.",
                        default="warning",
                        choices=["debug", "info", "warning", "error"],
                        type=str.lower)
    parser.add_argument("--log-file",
                        help="Where to store the log file. "
                             "Default: 'gog_installer_update_checker_{CURRENT_DATE}.log' "
                             "in the current working directory.",
                        nargs="?",
                        default=None)
    parser.add_argument("--data-file",
                        help="Path to the data file. "
                             "By default data.json in the current working directory is loaded if found, "
                             "otherwise nothing.",
                        nargs="?",
                        required=False,
                        default=os.path.join(os.getcwd(), "data.json"))

    args = parser.parse_args()

    paths = args.path  # type: List[str]
    innoextract_path = args.innoextract_path
    output_file = args.output_file
    data_file = args.data_file

    log_file = args.log_file

    if log_file is None:
        log_file = os.path.join(os.getcwd(), f"gog_installer_update_checker_{CURRENT_DATE}.log")
    else:
        dirname = os.path.dirname(log_file)
        basename, ext = os.path.splitext(log_file)
        log_file = os.path.join(dirname, f"{basename}_{CURRENT_DATE}{ext}")

    file_handler = logging.FileHandler(filename=log_file, encoding="utf-8")
    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    handlers = [file_handler, stdout_handler]

    log_level = logging.getLevelName(args.log_level.upper())

    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s: %(message)s", handlers=handlers)

    error_handler = ErrorHandler()

    if innoextract_path is None:
        logging.error("Innoextract not found in PATH and not specified. Exiting...")
        exit(1)

    if not os.path.exists(innoextract_path):
        logging.error("Innoextract path does not exist.")
        exit(1)

    if not os.path.isfile(innoextract_path):
        logging.error("Innoextract path is not a file.")
        exit(1)

    for path in paths:
        if not os.path.exists(path):
            logging.error(f"Path {path} does not exist.")
            exit(1)
        if not os.path.isdir(path):
            logging.error(f"Path {path} is not a directory.")
            exit(1)

    logging.info("Starting update checking...")

    set_data_content(data_file=data_file)

    start_processing(paths=paths,
                     innoextract_path=innoextract_path,
                     output_file=output_file)

    logging.shutdown()
    if os.lstat(log_file).st_size == 0:
        os.remove(log_file)

    if error_handler.fired:
        exit(1)
    else:
        exit(0)


def set_data_content(data_file: str) -> None:
    global DATA_FILE_CONTENT
    global STR_TO_REMOVE_FROM_NAME
    global REPLACE_NAMES
    global MATCH_VERSIONS
    global ROMAN_NUMERALS
    global GOODIES_ID
    global DELISTED_GAMES

    logging.info("Loading data file content...")

    try:
        with open(data_file, "r", encoding="utf_8") as f:
            DATA_FILE_CONTENT = json.load(f)
    except FileNotFoundError:
        logging.warning(f"File \"{data_file}\" doesn't exist. Loading empty content.")
        DATA_FILE_CONTENT = {}
    except json.decoder.JSONDecodeError as e:
        logging.error(f"Error decoding JSON data file \"{data_file}\".")
        logging.error(e)
        exit(1)

    if len(DATA_FILE_CONTENT) > 0:

        logging.info("Setting up constants...")

        # setup STR_TO_REMOVE_FROM_NAME

        if isinstance(DATA_FILE_CONTENT.get("Strings_To_Remove"), list):
            STR_TO_REMOVE_FROM_NAME = tuple(DATA_FILE_CONTENT.get("Strings_To_Remove"))
        elif DATA_FILE_CONTENT.get("Strings_To_Remove") is None:
            STR_TO_REMOVE_FROM_NAME = tuple()
        else:
            logging.warning(f"Content type of \"Strings_To_Remove\" is invalid, it should be a list and currently is "
                            f"{type(DATA_FILE_CONTENT.get("Strings_To_Remove"))}")
            STR_TO_REMOVE_FROM_NAME = tuple()

        # setup REPLACE_NAMES

        if isinstance(DATA_FILE_CONTENT.get("Replace_Names"), dict):
            REPLACE_NAMES = DATA_FILE_CONTENT.get("Replace_Names")
        elif DATA_FILE_CONTENT.get("Replace_Names") is None:
            REPLACE_NAMES = {}
        else:
            logging.warning(f"Content type of \"Replace_Names\" is invalid, it should be a dict and currently is "
                            f"{type(DATA_FILE_CONTENT.get("Replace_Names"))}")
            REPLACE_NAMES = {}

        # setup MATCH_VERSIONS

        if isinstance(DATA_FILE_CONTENT.get("Match_Versions"), dict):
            MATCH_VERSIONS = DATA_FILE_CONTENT.get("Match_Versions")
        elif DATA_FILE_CONTENT.get("Match_Versions") is None:
            MATCH_VERSIONS = {}
        else:
            logging.warning(f"Content type of \"Match_Versions\" is invalid, it should be a dict and currently is "
                            f"{type(DATA_FILE_CONTENT.get("Match_Versions"))}")
            MATCH_VERSIONS = {}

        # setup ROMAN_NUMERALS

        if isinstance(DATA_FILE_CONTENT.get("Roman_Numerals"), dict):
            ROMAN_NUMERALS = DATA_FILE_CONTENT.get("Roman_Numerals")
        elif DATA_FILE_CONTENT.get("Roman_Numerals") is None:
            ROMAN_NUMERALS = {}
        else:
            logging.warning(f"Content type of \"Roman_Numerals\" is invalid, it should be a dict and currently is "
                            f"{type(DATA_FILE_CONTENT.get("Roman_Numerals"))}")
            ROMAN_NUMERALS = {}

        # setup GOODIES_ID

        if isinstance(DATA_FILE_CONTENT.get("Goodies_ID"), dict):
            GOODIES_ID = DATA_FILE_CONTENT.get("Goodies_ID")
        elif DATA_FILE_CONTENT.get("Goodies_ID") is None:
            GOODIES_ID = {}
        else:
            logging.warning(f"Content type of \"Goodies_ID\" is invalid, it should be a dict and currently is "
                            f"{type(DATA_FILE_CONTENT.get("Goodies_ID"))}")
            GOODIES_ID = {}

        # setup DELISTED_GAMES

        if isinstance(DATA_FILE_CONTENT.get("Delisted_Games"), list):
            DELISTED_GAMES = DATA_FILE_CONTENT.get("Delisted_Games")
        elif DATA_FILE_CONTENT.get("Delisted_Games") is None:
            DELISTED_GAMES = {}
        else:
            logging.warning(f"Content type of \"Delisted_Games\" is invalid, it should be a list and currently is "
                            f"{type(DATA_FILE_CONTENT.get("Delisted_Games"))}")
            DELISTED_GAMES = {}
    else:
        logging.info("No data file or data file empty, setting empty constants...")

        STR_TO_REMOVE_FROM_NAME = tuple()
        REPLACE_NAMES = {}
        MATCH_VERSIONS = {}
        ROMAN_NUMERALS = {}
        GOODIES_ID = {}
        DELISTED_GAMES = {}


def start_processing(paths: List[str],
                     innoextract_path: str,
                     output_file: Optional[str]) -> None:
    logging.info("Starting processing...")
    logging.info("Paths:")
    for path in paths:
        logging.info("\t" + path)
    logging.info(f"InnoExtract path: \"{innoextract_path}\"")

    logging.info("Getting installers list...")
    installers_list = get_installers_list(paths=paths)

    if len(installers_list) == 0:
        logging.critical("No installers found, exiting...")
        exit(1)

    logging.info("Finished getting installers.")

    logging.info("Mapping installers to their product ID...")
    installers_dict = map_product_id(installers_list=installers_list,
                                     innoextract_path=innoextract_path)
    logging.info("Finished mapping installers.")

    logging.info("De-duplicating installers...")
    installers_dict = dedup_installers_id(installers_dict=installers_dict)
    logging.info("Finished de-duplicating installers.")

    logging.info("Retrieving information for the installers and removing non-base game installers...")
    local_info = insert_missing_info(installers_dict=installers_dict,
                                     innoextract_path=innoextract_path)
    logging.info("Finished retrieving information...")

    logging.info("Retrieving updated installer data from GOG...")
    online_info = get_online_data(local_info=local_info)
    logging.info("Finished retrieving updated data.")

    logging.info("Comparing installer versions...")
    new_versions_dict = {}
    compare_versions(local_info=local_info,
                     online_info=online_info,
                     new_versions_dict=new_versions_dict)
    logging.info("Finished comparing installer versions.")

    if len(new_versions_dict) > 0:
        if output_file is not None:
            logging.info("Dumping installers with available updated versions to output file...")
            write_installer_list(new_versions_dict=new_versions_dict,
                                 output_file=output_file)
            logging.info("Finished dumping installers.")
        else:
            logging.info("No output file set, list of updated installers wont be stored.")
    else:
        logging.info("No updated installers were found.")


def write_installer_list(new_versions_dict: dict,
                         output_file: str) -> None:
    dirname = os.path.dirname(output_file)
    name, ext = os.path.splitext(os.path.basename(output_file))

    name = f"{name}_{CURRENT_DATE}"

    output_file = os.path.join(dirname, name + ext)

    try:
        file_stream = open(output_file, "w", encoding="utf-8")
    except PermissionError as e:
        logging.critical("Couldn't open output file.")
        logging.critical(e)
        exit(1)

    for product_id in new_versions_dict.keys():
        product_name = new_versions_dict[product_id].get("product_name")
        local_version = new_versions_dict[product_id].get("local_version", UNKNOWN)
        local_build = new_versions_dict[product_id].get("local_build", UNKNOWN)
        online_version = new_versions_dict[product_id].get("online_version", UNKNOWN)
        online_build = new_versions_dict[product_id].get("online_build", UNKNOWN)

        local_old_version = new_versions_dict[product_id].get("local_old_version")  # type: bool
        online_old_version = new_versions_dict[product_id].get("online_old_version")  # type: bool

        file_stream.write(f"{product_name} ({product_id})" + "\n")

        if local_old_version and not online_old_version:
            file_stream.write(f"{local_version} {{OLD GEN INSTALLER}} -> {online_version}" + "\n")
        else:
            file_stream.write(f"{local_version} -> {online_version}" + "\n")

        file_stream.write(f"{local_build} -> {online_build}" + "\n")
        file_stream.write("\n\n")


def get_installers_list(paths: List[str]) -> List[str]:
    installer_list_aux = []
    installers_list = []

    logging.info("Retrieving executables from given paths...")

    for path in paths:
        executable_list = glob.glob("**" + os.path.sep + "*.exe", root_dir=path, recursive=True)

        if len(executable_list) == 0:
            logging.info(f"No executables found in \"{path}\".")

        for item in executable_list:
            installer_list_aux.append(os.path.join(path, item))

    if len(installer_list_aux) == 0:
        logging.error("No executables found in any of the paths.")
        return []

    for item in sorted(installer_list_aux):
        if re.search(INSTALLER_REGEX, item, re.IGNORECASE):
            installers_list.append(item)

    return copy.deepcopy(sorted(installers_list))


def map_product_id(installers_list: List[str],
                   innoextract_path: str) -> Dict[str, str]:
    mapping = {}
    for installer in sorted(installers_list):
        basename = os.path.basename(installer)

        logging.info(f"Retrieving product ID for \"{basename}\"...")

        product_id = get_product_id(installer_path=installer,
                                    innoextract_path=innoextract_path)

        if product_id is not None:
            mapping[installer] = product_id
            logging.info(f"Product ID for \"{basename}\" retrieved successfully.")
            logging.info(f"Product ID: {product_id}")

    return copy.deepcopy(mapping)


def get_product_id(installer_path: str,
                   innoextract_path: str) -> Optional[str]:
    global REPLACE_NAMES
    global DELISTED_GAMES

    basename = os.path.basename(installer_path)

    cmd = [innoextract_path,
           "-l",
           installer_path]

    try:
        output = subprocess.run(cmd, encoding="utf_8", capture_output=True, check=True).stdout.strip()
    except subprocess.CalledProcessError as e:
        logging.critical("Couldn't get the product id, there was an error executing innoextract.")
        logging.critical(f"ERROR:\n{e}")
        exit(1)

    product_id = None

    for line in output.splitlines():
        try:
            product_id = re.match(PRODUCT_ID_REGEX_1, line, re.IGNORECASE).groups()[0]
            break
        except (AttributeError, IndexError):
            pass

        try:
            product_id = re.match(PRODUCT_ID_REGEX_2, line, re.IGNORECASE).groups()[0]
            break
        except (AttributeError, IndexError):
            pass

    if product_id is not None:
        return product_id

    # try by retrieving the product name from the properties and searching it on GOG

    logging.info("Could not find the product ID with innoextract. Falling to search for product name on GOG and "
                 "retrieve the product ID from the response.")

    exe_info = get_exe_info(installer_path)

    if exe_info is not None:
        product_name = exe_info.get("ProductName")

        if product_name is None:
            logging.warning(f"Couldn't get the product name & ID for \"{basename}\". Please report this.")
            return None

        if product_name in DELISTED_GAMES:
            logging.info(f"Delisted game: {product_name}, skipping...")
            return None

        for string in STR_TO_REMOVE_FROM_NAME:
            product_name = re.sub(string, "", product_name, flags=re.IGNORECASE)

        if len(REPLACE_NAMES) > 0:
            product_name = REPLACE_NAMES.get(product_name, REPLACE_NAMES.get(exe_info.get("ProductName"), product_name))

        while re.search(r"[a-zá-úñ]-", product_name, flags=re.IGNORECASE):
            product_name = re.sub(r"([a-zá-úñ])-\s", r"\1 - ", product_name, flags=re.IGNORECASE)
    else:
        logging.warning(f"Couldn't get the product ID for \"{basename}\". Please report this.")
        return None

    product_id = search_product_id_on_gog(product_name=product_name)

    return product_id


def search_product_id_on_gog(product_name: str) -> Optional[str]:
    logging.info(f"Performing public search to get the product ID of: {product_name}...")

    gog_url = "https://embed.gog.com/games/ajax/filtered?mediaType=game&search={0}"
    gog_info = download_data(gog_url=gog_url,
                             product_name=product_name)

    if gog_info is None:
        return None

    if gog_info.get("totalGamesFound") == 0:
        found_numeral = False
        for numeral in ROMAN_NUMERALS.keys():
            if numeral in product_name.split():
                found_numeral = True
                product_name = product_name.replace(numeral, str(ROMAN_NUMERALS[numeral]))

        if found_numeral:
            logging.info(f"Roman number found in \"{product_name}\", replacing with decimal equivalent and searching "
                         f"again...")
            return search_product_id_on_gog(product_name)
        else:
            logging.warning(f"No games found for \"{product_name}\".")
            return None

    if gog_info.get("totalGamesFound") == 1:
        logging.info(f"A single game found for \"{product_name}\".")
        return str(gog_info["products"][0]["id"])

    for product in gog_info["products"]:
        if product_name.lower() == product["title"].lower():
            logging.info(f"Product ID for \"{product_name}\" found.")
            return str(product["id"])

    logging.warning(f"No games found for \"{product_name}\".")
    return None


def get_exe_info(file_path: str) -> Optional[dict]:
    """
    | Available keys (their value may or may not be Nonetype):

    - *Comments*
    - *InternalName*
    - *CompanyName*
    - *FileDescription*
    - *FileVersion*
    - *LegalCopyright*
    - *OriginalFileName*
    - *ProductName*
    - *ProductVersion*
    - *LegalTrademarks*
    - *PrivateBuild*
    - *SpecialBuild*

    :param file_path: Path to executable file.
    :return: Dictionary with information about the executable file.
    """

    logging.info(f"Extracting information from executable file: {os.path.basename(file_path)}")

    if global_exe_info.get(file_path, "") != "":
        logging.info(f"Information for executable file: \"{file_path}\" was found, reusing...")
        return copy.deepcopy(global_exe_info[file_path])

    properties = ("Comments", "InternalName", "ProductName", "CompanyName", "LegalCopyright", "ProductVersion",
                  "FileDescription", "LegalTrademarks", "PrivateBuild", "FileVersion", "OriginalFilename",
                  "SpecialBuild")

    try:
        lang, codepage = win32api.GetFileVersionInfo(file_path, "\\VarFileInfo\\Translation")[0]
        properties_dict = {}

        for property_name in properties:
            info_path = "\\StringFileInfo\\{:04X}{:04X}\\{}".format(lang, codepage, property_name)

            try:
                key = property_name.strip()
            except AttributeError:
                key = property_name
                if key is None:
                    continue

            try:
                value = win32api.GetFileVersionInfo(file_path, info_path).strip()
            except AttributeError:
                value = win32api.GetFileVersionInfo(file_path, info_path)

            if value == "":
                value = None

            properties_dict[key] = value

        return copy.deepcopy(properties_dict)
    except:
        return None


def dedup_installers_id(installers_dict: dict) -> Dict[str, Dict[str, str]]:
    id_list = set()

    for key in sorted(installers_dict.keys()):
        id_list.add(str(installers_dict[key]))

    deduped_installers_dict_aux = {}

    for product_id in sorted(id_list):
        found = False

        while not found:
            for key in sorted(installers_dict.keys()):  # type: str
                if installers_dict[key] == product_id:
                    found = True
                    deduped_installers_dict_aux[key] = {"product_id": product_id}
                    break

    deduped_installers_dict = {}

    for item in sorted(deduped_installers_dict_aux.keys()):
        deduped_installers_dict[item] = copy.deepcopy(deduped_installers_dict_aux[item])

    return copy.deepcopy(deduped_installers_dict)


def insert_missing_info(installers_dict: dict,
                        innoextract_path: str) -> dict:
    """
    Insert missing installer information into the installers_dict.

    :return: A new dictionary containing the complete installer information
    """

    local_info = copy.deepcopy(installers_dict)

    for installer in sorted(installers_dict.keys()):
        basename = os.path.basename(installer)

        logging.info(f"Processing \"{basename}\"...")

        if re.search(OLD_VERSION_REGEX, installer, re.IGNORECASE):
            logging.info(f"\"{basename}\" detected as old gen.")
            old_installer = True
        else:
            logging.info(f"\"{basename}\" detected as current gen.")
            old_installer = False

        product_id = installers_dict[installer]["product_id"]

        logging.info("Retrieving installer information from file properties...")

        local_info.update(get_local_info_from_exe(file_path=installer,
                                                  old_installer=old_installer))
        local_info[installer].update({"product_id": product_id})

        logging.info("Finished retrieving installer information from file properties.")

        tmp_dir = tempfile.mkdtemp()

        logging.info("Starting extraction of info file from installer...")

        if old_installer:
            info_file = extract_info_file_old(product_id=product_id,
                                              innoextract_path=innoextract_path,
                                              tmp_dir=tmp_dir,
                                              file_path=installer)
        else:
            info_file = extract_info_file(product_id=product_id,
                                          innoextract_path=innoextract_path,
                                          tmp_dir=tmp_dir,
                                          file_path=installer)

        if info_file is None:
            continue

        logging.info("Finished extraction of info file.")

        # When using 7-Zip to extract the info file, the file is extracted with the original directory structure which
        # might be a subdirectory, in this case we have to move the info file to the root of the temporary directory.
        move_info_file_to_root(tmp_dir=tmp_dir)

        logging.info("Reading info file...")

        out_stream = open(os.path.join(tmp_dir, info_file), mode="r", encoding="utf_8", errors="backslashreplace")
        info_file_content = json.load(out_stream)
        out_stream.close()

        logging.info("Checking for non-base game installer")

        if not is_main_game(installer_info=info_file_content):
            logging.info("Installer is not a base game installer, removing from update checking...")
            local_info.pop(installer)
            continue

        logging.info("Filling all the missing info for the installer...")

        if old_installer:
            if local_info[installer].get("version_name") is None:
                logging.info("Trying to retrieve the version from the filename...")

                version_name = get_old_version_from_filename(filename=os.path.basename(installer))
                if version_name is not None:
                    # If the version_name variable is None, we don't need to do anything as that's the fallback value
                    # when getting the info from the exe properties.
                    local_info[installer]["version_name"] = version_name

            if info_file_content.get("name") is not None:
                # Game's name from the info file is preferred against the ProductName property of the executable file
                local_info[installer]["product_name"] = info_file_content["name"]
        else:
            if local_info[installer].get("build_id") is None:
                try:
                    local_info[installer]["build_id"] = info_file_content["buildId"]
                except KeyError:
                    logging.info("Build ID not found in info file and file properties.")

            if local_info[installer].get("version_name") is None:
                logging.info("Trying to retrieve the version from the filename...")

                local_info[installer]["version_name"] = get_version_from_filename(filename=os.path.basename(installer))

                if local_info[installer].get("version_name") is None:
                    logging.info("Could not get the version from the filename.")

            if info_file_content.get("name") is not None:
                local_info[installer]["product_name"] = info_file_content["name"]

        logging.info("Finished filling missing info for the installer.")

        try:
            shutil.rmtree(tmp_dir)
        except (FileNotFoundError, PermissionError):
            pass

        logging.info(f"Finished processing \"{basename}\".")

    return copy.deepcopy(local_info)


def get_local_info_from_exe(file_path: str,
                            old_installer: bool) -> dict:

    exe_info = get_exe_info(file_path=file_path)

    product_name = exe_info["ProductName"]

    if re.match(BUILD_ID_REGEX, exe_info["ProductVersion"]):
        build_id = re.search(BUILD_ID_REGEX, exe_info["ProductVersion"]).groups()[0]
    else:
        build_id = None
    if not old_installer:
        if re.match(VERSION_NAME_REGEX, exe_info["ProductVersion"]):
            version_name = re.search(VERSION_NAME_REGEX, exe_info["ProductVersion"]).groups()[0]
        else:
            version_name = None
    else:
        version_name = exe_info["ProductVersion"]

    info_dict = {file_path: {
        "build_id": build_id,
        "product_name": product_name,
        # old_version refers to the installer, not the game version.
        "old_version": old_installer,
        # If both local and online installers are old versions, we use the version name found in the filename to
        # compare versions, so we have to retrieve and store this.
        "version_name": version_name
    }}

    return copy.deepcopy(info_dict)


def extract_info_file(product_id: str,
                      innoextract_path: str,
                      tmp_dir: str,
                      file_path: str) -> Optional[str]:
    """
    Extract the info file from the given installer to the specified temporary directory,
    returning the name of the info file.

    :param product_id: ID of the product to extract the info from
    :param innoextract_path: Path to innoextract
    :param tmp_dir: Temporary directory where to extract the info file
    :param file_path: Installer path

    :return: The name of the info file or *None* if no info file could be extracted
    """

    info_file = "goggame-" + product_id + ".info"

    cmd_extract = [innoextract_path,
                   "-e",
                   "-I",
                   info_file,
                   "-d",
                   tmp_dir,
                   file_path]

    try:
        subprocess.run(cmd_extract, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        print(e)
        return None

    if len(os.listdir(tmp_dir)) != 0:
        return info_file
    else:
        logging.info("Couldn't extract info file from installer.")
        return None


def extract_info_file_old(product_id: str,
                          innoextract_path: str,
                          tmp_dir: str,
                          file_path: str) -> Optional[str]:
    """
    Extract the info file from the given old gen installer to the specified temporary directory,
    returning the name of the info file.

    :param product_id: ID of the product to extract the info from
    :param innoextract_path: Path to innoextract
    :param tmp_dir: Temporary directory where to extract the info file
    :param file_path: Installer path

    :return: The name of the info file or *None* if no info file could be extracted
    """

    file_basename = os.path.basename(file_path).replace(".exe", "")
    file_parent = os.path.dirname(file_path)

    bin_list = glob.glob(file_basename + "-*.bin", root_dir=file_parent)

    if len(bin_list) == 0:
        bin_list = glob.glob(file_basename + ".bin", root_dir=file_parent)

    if len(bin_list) != 0:
        info_file = "goggame-{}.info".format(product_id)
        info_file_in_bin = "game\\" + info_file

        password = md5(product_id.encode()).hexdigest()

        cmd_extract_orig = ["7z",
                            "e",
                            "",
                            "-o" + tmp_dir,
                            info_file_in_bin,
                            "-aoa",
                            "-y",
                            "-p" + password]

        bin_path = os.path.join(file_parent, bin_list[0])

        cmd_extract = copy.deepcopy(cmd_extract_orig)
        cmd_extract[2] = bin_path

        subprocess.run(cmd_extract, capture_output=True)

        if len(os.listdir(tmp_dir)) != 0:
            return info_file
        else:
            return None
    else:
        logging.info("Installer is not using the old RAR compression, falling to innoextract...")
        return extract_info_file(product_id=product_id,
                                 innoextract_path=innoextract_path,
                                 tmp_dir=tmp_dir,
                                 file_path=file_path)


def move_info_file_to_root(tmp_dir: str) -> None:
    logging.info("Checking if info file is not located in the root of the temporary directory...")

    file_path = glob.glob("**\\*.info", root_dir=tmp_dir, recursive=True)[0]
    file_path = os.path.join(tmp_dir, file_path)

    correct_path = os.path.join(tmp_dir, os.path.split(file_path)[1])

    if file_path != correct_path:
        logging.info("Moving info file to the root...")
        import shutil
        shutil.move(file_path, tmp_dir)

        logging.info("Removing subdirectory from the temp directory...")

        path_to_remove = os.path.split(file_path)[0]

        while True:
            if os.path.split(path_to_remove)[0] == tmp_dir:
                break
            path_to_remove = os.path.split(path_to_remove)[0]

        shutil.rmtree(path_to_remove)
    else:
        logging.info("Nothing to move.")


def is_main_game(installer_info: dict) -> bool:
    """
    Detect if the installer belongs to a main game and not a DLC/Expansion/Goodie.

    :param installer_info: Installer information
    :return: Whether the installer belongs to a main game
    """
    global GOODIES_ID

    if installer_info.get("dependencyGameId") is not None:
        if installer_info.get("dependencyGameId") != "":
            return False
        else:
            return True
    elif installer_info.get("gameId") != installer_info.get("rootGameId"):
        return False
    elif str(installer_info.get("rootGameId")) in GOODIES_ID.keys():
        return False
    else:
        return True


def get_old_version_from_filename(filename: str):
    """
    Try to extract the version from the filename for old gen installers.

    :param filename: Filename where to extract the version from

    :return: Version string if found, else *None*
    """
    try:
        version_name = re.search(OLD_VERSION_REGEX, filename, re.IGNORECASE).groups()[0]
    except (AttributeError, IndexError):
        version_name = None
        logging.error("Couldn't get the version from the filename.")

    return version_name


def get_version_from_filename(filename: str) -> Optional[str]:
    """
    | Try to extract the version from the filename.
    | Versioning (at least in games) varies wildly, so it's extremely complicated to extract the correct version from
        the filename, which is why this is the last option.

    :param filename: Filename where to extract the version from

    :return: Version string if found, else *None*
    """

    filename = os.path.basename(filename)

    try:
        version_name = re.search(EXTRACT_VERSION_REGEX, filename).groups()[0]
    except (AttributeError, IndexError, ValueError, TypeError):
        version_name = None
        logging.warning(f"Could not extract version from \"{filename}\"")

    if version_name is not None:
        version_name = version_name.strip().replace("_", " ")

    return version_name


def get_online_data(local_info) -> dict:

    online_info = {}

    for installer in sorted(local_info.keys()):
        basename = os.path.basename(installer)

        logging.info(f"Downloading updated data for \"{basename}\"...")

        product_id = local_info[installer]["product_id"]

        load_online_data(product_id=product_id,
                         online_info=online_info,
                         file_path=installer)

        logging.info(f"Finished downloading data for \"{basename}\".")

    return copy.deepcopy(online_info)


def load_online_data(product_id: str,
                     online_info: dict,
                     file_path: str) -> None:
    logging.info(f"Retrieving latest build and version for \"{product_id}\"...")

    gog_url = "https://content-system.gog.com/products/{0}/os/windows/builds?generation=2"

    gog_dict = download_data(product_id=product_id,
                             gog_url=gog_url)

    if gog_dict is None:
        online_info[product_id] = {"version_name": None,
                                   "build_id": None,
                                   "old_version": None}
        return

    if gog_dict["count"] == 0:
        logging.info("Trying to find if the product ID belongs to a pack and extract the (actual) game ID...")

        new_product_id = get_product_id_from_pack(product_id=product_id)

        if new_product_id is not None and new_product_id != product_id:
            gog_dict = download_data(product_id=new_product_id,
                                     gog_url=gog_url)

    if gog_dict["count"] == 0:
        logging.warning(f"Product \"{os.path.basename(file_path)}\" ({product_id}) build information wasn't found on "
                        f"GOG.")
        online_info[product_id] = {"version_name": None,
                                   "build_id": None,
                                   "old_version": None}
        return

    if gog_dict["items"][0].get("legacy_build_id") is None:
        last_version = gog_dict["items"][0]["version_name"]
        last_build = gog_dict["items"][0]["build_id"]

        online_info[product_id] = {"version_name": last_version,
                                   "build_id": last_build,
                                   # old_version refers to the online installer version, not the game version
                                   "old_version": False}
    else:
        logging.info("Only old gen installers are available for this game.")

        last_legacy_build_id = str(gog_dict["items"][0].get("legacy_build_id"))

        # if the last online installer is old_version, then we have to get the version from the (online) filename, for
        # this we have to download the repository info.
        logging.info("Retrieving latest legacy installer version...")
        last_version = get_last_version_old_installer(last_legacy_build_id=last_legacy_build_id,
                                                      product_id=product_id)

        online_info[product_id] = {"version_name": last_version,
                                   "build_id": last_legacy_build_id,
                                   "old_version": True}

    logging.info("Finished retrieving latest build and version.")


def download_data(gog_url: str,
                  product_id: str = None,
                  legacy_build_id: str = None,
                  product_name: str = None) -> Optional[dict]:
    if legacy_build_id is not None and product_id is not None:
        logging.info(f"Downloading repository data for \"{product_id}\"...")
        gog_url = gog_url.format(product_id, legacy_build_id)
    elif product_name is not None:
        logging.info(f"Downloading search data for \"{product_name}\"...")
        gog_url = gog_url.format(product_name)
    elif product_id is not None:
        logging.info(f"Downloading game data for \"{product_id}\"...")
        gog_url = gog_url.format(product_id)
    else:
        raise ValueError("download_data() needs at least one of product_id or legacy_build_id or product_name.")

    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})

    api_response = sess.get(gog_url)

    if api_response.status_code != 200:
        logging.error(f"There was an error downloading the data for: {product_id}")
        logging.error(f"Status Code: {api_response.status_code}")
        return None

    api_response = api_response.content.decode(encoding="utf_8", errors="replace")
    gog_dict = json.loads(api_response)

    return copy.deepcopy(gog_dict)


def get_product_id_from_pack(product_id: str) -> Optional[str]:
    gog_url = "https://api.gog.com/v2/games/{0}?locale=en-US"

    gog_dict = download_data(product_id=product_id,
                             gog_url=gog_url)

    if gog_dict is None:
        return None

    try:
        product_type = gog_dict.get("_embedded").get("productType")
    except AttributeError:
        product_type = None

    if product_type is None or product_type != "PACK":
        return product_id

    try:
        new_url = gog_dict.get("_links").get("includesGames")[0].get("href").strip()
        product_id = re.search(r"https://api.gog.com/v2/games/([0-9]+)\?locale=en-US", new_url).groups()[0]
    except (AttributeError, IndexError):
        product_id = None

    return product_id


def get_last_version_old_installer(last_legacy_build_id: str,
                                   product_id: str) -> Optional[str]:
    gog_url = "https://cdn.gog.com/content-system/v1/manifests/{0}/windows/{1}/repository.json"

    gog_dict_old = download_data(product_id=product_id,
                                 gog_url=gog_url,
                                 legacy_build_id=last_legacy_build_id)

    if gog_dict_old is None:
        return None

    try:
        online_filename = gog_dict_old["product"]["support_commands"][0]["executable"]
    except AttributeError:
        return None

    try:
        online_version = re.search(OLD_VERSION_REGEX, online_filename).groups()[0]
    except (AttributeError, IndexError):
        online_version = None

    return online_version


def compare_versions(local_info: dict,
                     online_info: dict,
                     new_versions_dict: dict) -> None:
    local_info = sort_local_info(local_info=local_info)

    print("")
    for installer in local_info.keys():
        logging.info(f"Comparing versions for \"{os.path.basename(installer)}\"...")

        if local_info[installer]["old_version"]:
            compare_old_versions(local_installer_info=local_info[installer],
                                 online_info=online_info,
                                 new_versions_dict=new_versions_dict)
        else:
            compare_new_versions(local_installer_info=local_info[installer],
                                 online_info=online_info,
                                 new_versions_dict=new_versions_dict)


def sort_local_info(local_info: dict) -> dict:
    """
    Sort installers by product_name

    :returns: Sorted installers
    """
    logging.info("Sorting local information by product name...")

    sorted_local_info = {}
    installer_name_map = {}

    for installer in sorted(local_info.keys()):
        product_name = local_info[installer]["product_name"]
        installer_name_map[product_name] = installer

    for prod_name in sorted(installer_name_map.keys()):
        installer = installer_name_map[prod_name]

        sorted_local_info.update({installer: local_info[installer]})

    return copy.deepcopy(sorted_local_info)


def compare_new_versions(local_installer_info: dict,
                         online_info: dict,
                         new_versions_dict: dict) -> None:
    """
    Compare the local installer information and the online installer information for current gen installers.

    :param local_installer_info: Local installer information
    :param online_info: Online installer information
    :param new_versions_dict: Dictionary where to store new versions
    """
    product_id = local_installer_info["product_id"]
    product_name = local_installer_info["product_name"]

    online_version = online_info[product_id]["version_name"]

    local_version = local_installer_info["version_name"]

    try:
        online_build = int(online_info[product_id]["build_id"])
    except TypeError:
        online_build = UNKNOWN

    try:
        local_build = int(local_installer_info["build_id"])
    except TypeError:
        local_build = UNKNOWN

    if online_build != UNKNOWN and local_build != UNKNOWN:
        if online_build > local_build:
            logging.info("Online build is newer than local build, updated installer found!")

            if local_version is None or local_version == UNKNOWN:
                local_version = get_local_version_from_gog(str(local_build),
                                                           product_id)

            if local_version is None:
                local_version = UNKNOWN

            new_versions_dict[product_id] = {"product_name": product_name,
                                             "local_version": local_version,
                                             "local_build": local_build,
                                             "local_old_version": False,
                                             "online_version": online_version,
                                             "online_build": online_build,
                                             "online_old_version": False}

            print(f"{product_name} ({product_id}) : {local_version} ({local_build})"
                  f" -> {online_version} ({online_build})")
        else:
            logging.info("Online build is either older (which is highly unlikely) or same as local.")
    else:
        if local_version is None or local_version == UNKNOWN:
            logging.error(f"Can't compare versions for \"{product_name}\" ({product_id}). "
                          f"There is at least one build ID missing and local installer's version is also missing.")
            return

        if versions_should_match(product_id=product_id,
                                 local_version=local_version,
                                 online_version=online_version):
            return

        local_version_norm = normalize_version_name(local_version)
        online_version_norm = normalize_version_name(online_version)

        if local_version_norm.lower() != online_version_norm.lower():
            logging.info("Online version is newer than local version, updated installer found!")

            new_versions_dict[product_id] = {"product_name": product_name,
                                             "local_version": local_version,
                                             "local_build": local_build,
                                             "local_old_version": False,
                                             "online_version": online_version,
                                             "online_build": online_build,
                                             "online_old_version": False}

            print(f"{product_name} ({product_id}) : {local_version} ({local_build})"
                  f" -> {online_version} ({online_build})")
        else:
            logging.info("Online version is either older (which is highly unlikely) or same version as local.")


def versions_should_match(product_id: str,
                          local_version: str,
                          online_version: str) -> bool:
    """
    Check if product_id is present in Match_Versions of the data file, then check for the local and online versions
    in any list of product_id. If found, those two versions should be considered equal.

    :returns: True if both versions are found, else False
    """
    global MATCH_VERSIONS

    if len(MATCH_VERSIONS) == 0:
        return False

    if product_id in MATCH_VERSIONS.keys():
        for version_list in MATCH_VERSIONS[product_id]:
            if type(version_list) is not list:
                logging.error(f"Data type in JSON file is invalid, \"{product_id}\" should only contain a list of one "
                              f"or more lists.")
                continue
            if len(version_list) != 2:
                logging.error(f"List length in JSON file is invalid, \"{product_id}\" should only contain lists of "
                              f"two elements.")
                continue

            if online_version in version_list and local_version in version_list:
                logging.info(f"Local ({local_version}) and online ({online_version}) versions found in"
                             f"\"Match_Versions\" on the data file for \"{product_id}\", they will be assumed to be "
                             f"the same.")
                return True

    return False


def normalize_version_name(version_name: str) -> str:
    # This was done for the cases when the version has to be extracted from the filename where the online version might
    # have some of these characters, but they are illegal in Windows so the extracted version won't have them
    illegal_characters = ("#", "!", "?", "\\", "/", "~", "|", "&", "$")

    new_version_name = version_name

    for character in illegal_characters:
        while re.match(r"^" + re.escape(character) + "+.+$", new_version_name):
            new_version_name = re.sub(r"^" + re.escape(character) + "+(.+)$", r"\1", new_version_name)

        while re.match(r"^.+?" + re.escape(character) + "+$", new_version_name):
            new_version_name = re.sub(r"^(.+?)" + re.escape(character) + r"+$", r"\1", new_version_name)

    new_version_name = new_version_name.strip().replace("_", " ")

    while new_version_name.endswith("."):
        new_version_name = new_version_name[:-1]

    return new_version_name


def get_local_version_from_gog(local_build: str,
                               product_id: str) -> Optional[str]:
    """
    Try to get the version of the local installer directly from GOG.

    :param local_build: Build ID of the local installer
    :param product_id: Product ID of the local installer

    :return: Version of the local installer or *None* if no version could be found
    """

    gog_url = "https://content-system.gog.com/products/{0}/os/windows/builds?generation=2"

    gog_dict = download_data(product_id=product_id,
                             gog_url=gog_url)

    if gog_dict is None:
        return None

    for item in gog_dict["items"]:
        if item["build_id"] == local_build:
            return item["version_name"]

    return None


def compare_old_versions(local_installer_info: dict,
                         online_info: dict,
                         new_versions_dict: dict) -> None:
    """
    Compare the local installer information -which is old gen- and the online installer information that might or might
    not be old gen.

    :param local_installer_info: Local installer information
    :param online_info: Online installer information
    :param new_versions_dict: Dictionary where to store new versions
    """
    product_id = local_installer_info["product_id"]
    product_name = local_installer_info["product_name"]

    if online_info[product_id].get("old_version") is None:
        logging.info(f"Product ID \"{product_id}\" wasn't found online, nothing to compare. Skipping...")
        return

    online_version = online_info[product_id]["version_name"]
    local_version = local_installer_info["version_name"]

    if online_version is None:
        online_version = UNKNOWN

    if local_version is None:
        local_version = UNKNOWN

    local_build = local_installer_info["build_id"]
    online_build = online_info[product_id]["build_id"]

    if local_build is None:
        local_build = UNKNOWN

    if online_build is None:
        online_build = UNKNOWN

    if online_info[product_id]["old_version"]:
        if online_version == UNKNOWN:
            # means we couldn't download the required information, nothing to compare
            logging.info(f"Couldn't download repository data for \"{product_id}\", we can't compare versions, "
                         f"skipping...")
            return

        if versions_should_match(product_id=product_id,
                                 local_version=local_version,
                                 online_version=online_version):
            return

        if online_version != local_version:
            logging.info("Online version is newer than local version, updated installer found!")

            new_versions_dict[product_id] = {"product_name": product_name,
                                             "local_version": local_version,
                                             "local_build": local_build,
                                             "local_old_version": True,
                                             "online_version": online_version,
                                             "online_build": online_build,
                                             "online_old_version": True}

            print(f"{product_name} ({product_id}) : {local_version} -> {online_version}")
        else:
            logging.info("Online version is either older (which is highly unlikely) or same version as local.")
    else:
        logging.info("Online installer is current gen and local installer is old gen. Assuming an update "
                     "is available.")

        new_versions_dict[product_id] = {"product_name": product_name,
                                         "local_version": local_version,
                                         "local_build": local_build,
                                         "local_old_version": True,
                                         "online_version": online_version,
                                         "online_build": online_build,
                                         "online_old_version": False}

        print(f"{product_name} ({product_id}) : {local_version} {{OLD GEN INSTALLER}} -> {online_version}")


if __name__ == '__main__':
    main()
