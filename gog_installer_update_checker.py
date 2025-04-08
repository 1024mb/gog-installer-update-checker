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
from string import Template
from typing import Any

import requests
import win32api
from pydantic import BaseModel, ValidationError, field_validator
from requests import RequestException

from __init__ import __version__


class DataFile(BaseModel, validate_assignment=True):
    Match_Versions: dict[str, list[tuple[str, str]]] | None = None
    Replace_Names: dict[str, str] | None = None
    Strings_To_Remove: list[re.Pattern[str]] | None = None
    Roman_Numerals: dict[str, int] | None = None
    Goodies_ID: dict[str, str] | None = None
    Delisted_Games: list[str] | None = None

    @classmethod
    @field_validator("Strings_To_Remove", mode="before")
    def compile_patterns(cls,
                         value):
        if not isinstance(value, list):
            return value

        compiled_patterns = []
        for item in value:
            if isinstance(item, str):
                compiled_patterns.append(re.compile(item, flags=re.IGNORECASE))
            else:
                compiled_patterns.append(item)

        return compiled_patterns


class ExecutableInfo(BaseModel, validate_assignment=True):
    Comments: str | None
    InternalName: str | None
    ProductName: str | None
    CompanyName: str | None
    LegalCopyright: str | None
    ProductVersion: str | None
    FileDescription: str | None
    LegalTrademarks: str | None
    PrivateBuild: str | None
    FileVersion: str | None
    OriginalFilename: str | None
    SpecialBuild: str | None


class ErrorTrackHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.error_occurred = False

    def emit(self,
             record):
        if record.levelno >= logging.ERROR:
            self.error_occurred = True


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0"

INSTALLER_REGEX: re.Pattern = re.compile(
        r"(?:\\|/|^)setup((?:_+[A-Za-zÁ-Úá-úÑñ0-9\-.]+)+)(_.+)?(_+\([0-9]+\)|_[0-9]+(?:\.[0-9]+)+)(_\(["
        r"^\)]+\))?\.exe$",
        flags=re.IGNORECASE)
PRODUCT_ID_REGEX_1: re.Pattern = re.compile(r"^.+?\"tmp\\([0-9]+)\.ini\"",
                                            flags=re.IGNORECASE)
PRODUCT_ID_REGEX_2: re.Pattern = re.compile(r"^.+?\"(?:.+?\\)?goggame-([0-9]+)\.(?:hashdb|info|script|id)\"",
                                            flags=re.IGNORECASE)
OLD_VERSION_REGEX: re.Pattern = re.compile(r"_([0-9]+(?:\.[0-9]+)+)\.exe",
                                           flags=re.IGNORECASE)
EXTRACT_VERSION_REGEX: re.Pattern = re.compile(r"_+((?:v\.?)?(?:[a-zá-úñ0-9]+\-)?"
                                               r"(?:[0-9\-]+(?:\.[0-9a-z\-_]+?(?:\([^\)]+?\))?)*))_\(",
                                               flags=re.IGNORECASE)
BUILD_ID_REGEX: re.Pattern = re.compile(r".+?\.\[([0-9]+)\]",
                                        flags=re.IGNORECASE)
VERSION_NAME_REGEX: re.Pattern = re.compile(r"(.+?)\.(?:\[[0-9]*\]?)?$",
                                            flags=re.IGNORECASE)
PRODUCT_ID_FROM_URL_REGEX: re.Pattern = re.compile(r"https://api.gog.com/v2/games/([0-9]+)\?locale=en-US",
                                                   flags=re.IGNORECASE)

UNKNOWN: str = "Unknown"
CURRENT_DATE: str = datetime.today().strftime("%Y%m%d_%H%M%S")

global_exe_info: dict[str, ExecutableInfo] = {}

DATA: DataFile

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT
})

INNOEXTRACT_PATH: str | None
SEVENZIP_PATH: str | None


# TODO:
#  - Store API responses to use with get_local_version_from_gog() instead of re-downloading it.
#  - Maybe add a function to retrieve account games for delisted games, mainly for old gen installers as we rely on
#    public search to get their product ID.


def get_program_dir() -> str:
    try:
        return os.path.abspath(__compiled__.containing_dir)
    except NameError:
        return os.path.abspath(os.path.dirname(sys.argv[0]))


PROGRAM_DIR = get_program_dir()


def main():
    parser = argparse.ArgumentParser(prog="gog-installer-update-checker",
                                     description="Check GOG installer for updates")
    parser.add_argument("-v", "--version",
                        action="version",
                        version=f"%(prog)s v{__version__}")
    parser.add_argument("--path",
                        help="Path(s) to directories containing GOG installers.",
                        nargs="+",
                        required=True,
                        action="extend")
    parser.add_argument("--innoextract-path",
                        help="Path to the innoextract executable. By default taken from PATH.",
                        default=shutil.which("innoextract"))
    parser.add_argument("--seven-zip-path",
                        help="Path to the 7-zip executable. By default taken from PATH.",
                        default=shutil.which("7z"))
    parser.add_argument("--output-file",
                        help="Path to the file where the installers with found updates will be listed. Current date is "
                             "appended to the name. Default is no output file.",
                        required=False,
                        type=str)
    parser.add_argument("--data-file",
                        help="Path to the data file. "
                             "By default data.json in the app directory is loaded if found, "
                             "otherwise nothing.",
                        type=str,
                        default=os.path.join(PROGRAM_DIR, "data.json"))
    parser.add_argument("--log-level",
                        help="How much stuff is logged. Can be 'debug', 'info', 'warning', 'error'.",
                        default="warning",
                        choices=["debug", "info", "warning", "error"],
                        type=str.lower)
    parser.add_argument("--log-file",
                        help="Where to store the log file. "
                             "Default: 'gog_installer_update_checker_{CURRENT_DATE}.log' in the current working "
                             "directory.",
                        required=False,
                        type=str)

    args = parser.parse_args()

    global INNOEXTRACT_PATH, SEVENZIP_PATH

    INNOEXTRACT_PATH = args.innoextract_path
    SEVENZIP_PATH = args.seven_zip_path

    paths: list[str] = args.path
    output_file: str | None = args.output_file
    data_file: str = args.data_file
    log_file: str | None = args.log_file
    log_level: int = logging.getLevelName(args.log_level.upper())

    if log_file is None:
        log_file = os.path.join(PROGRAM_DIR, f"gog_installer_update_checker_{CURRENT_DATE}.log")
    else:
        log_file = os.path.abspath(log_file)
        dirname = os.path.dirname(log_file)
        basename, ext = os.path.splitext(log_file)
        log_file = os.path.join(dirname, f"{basename}_{CURRENT_DATE}{ext}")

    file_handler = logging.FileHandler(filename=log_file, encoding="utf-8")

    if log_level > logging.WARNING:
        file_handler.setLevel("WARNING")
    else:
        file_handler.setLevel(log_level)

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setLevel(log_level)

    error_handler = ErrorTrackHandler()
    handlers = [file_handler, stdout_handler, error_handler]

    if log_level > logging.WARNING:
        logger_level = logging.WARNING
    else:
        logger_level = log_level

    logging.basicConfig(level=logger_level, format="[%(asctime)s] [%(levelname)s]: %(message)s", handlers=handlers)

    tools: tuple[tuple[str | None, str], tuple[str | None, str]] = (
        (INNOEXTRACT_PATH, "innoextract"),
        (SEVENZIP_PATH, "7-zip")
    )

    for tool, name in tools:
        if tool is None:
            logging.critical(f"{name} not found in PATH and not specified. Exiting...")
            sys.exit(1)

        if not os.path.exists(tool):
            logging.critical(f"{name} path does not exist.")
            sys.exit(1)

        if not os.path.isfile(tool):
            logging.critical(f"{name} path is not a file. It should be the path to the executable")
            sys.exit(1)

    for path in paths:
        if not os.path.exists(path):
            logging.critical(f"Path {path} does not exist.")
            sys.exit(1)

        if not os.path.isdir(path):
            logging.critical(f"Path {path} is not a directory.")
            sys.exit(1)

    logging.info("Starting update checking...")

    global DATA
    DATA = get_data_content(data_file=data_file)

    start_processing(paths=paths,
                     output_file=output_file)

    logging.shutdown()

    if error_handler.error_occurred:
        sys.exit(1)
    else:
        sys.exit(0)


def get_data_content(data_file: str) -> DataFile:
    logging.info("Loading data file content...")

    try:
        with open(data_file, "r", encoding="utf_8") as f:
            data_file_content: dict[Any, Any] = json.load(f)
            logging.debug(f"Data file content:\n{json.dumps(data_file_content, indent=4)}")
    except FileNotFoundError:
        logging.warning(f"File \"{data_file}\" doesn't exist. Loading empty content.")
        return DataFile()
    except json.decoder.JSONDecodeError:
        logging.error(f"Error decoding JSON data file \"{data_file}\":", exc_info=True)
        sys.exit(1)

    if len(data_file_content) > 0:
        logging.info("Validating data file...")

        try:
            return DataFile.model_validate(data_file_content)
        except ValidationError:
            logging.critical("Error validating data file.", exc_info=True)
            sys.exit(1)
    else:
        logging.info("Data file empty.")
        return DataFile()


def start_processing(paths: list[str],
                     output_file: str | None) -> None:
    logging.info("Starting processing...")
    logging.debug("Paths:")
    logging.debug("\t" + "\n\t".join(paths))
    logging.debug(f"InnoExtract path: \"{INNOEXTRACT_PATH}\"")
    logging.debug(f"7-zip path: \"{SEVENZIP_PATH}\"")

    logging.info("Getting installers list...")
    installers_list: tuple[str, ...] = get_installers_list(paths=paths)

    if len(installers_list) == 0:
        logging.critical("No installers found, exiting...")
        sys.exit(1)

    logging.info("Finished getting installers.")

    logging.info("Mapping installers to their product ID...")
    installers_dict: dict[str, str] = map_product_id(installers_list=installers_list)
    logging.info("Finished mapping installers.")

    logging.info("De-duplicating installers...")
    installers_dict_deduped: dict[str, dict[str, str]] = dedup_installers_id(installers_dict=installers_dict)
    logging.info("Finished de-duplicating installers.")

    logging.info("Retrieving information for the installers and removing non-base game installers...")
    local_info: dict[str, dict[str, str | bool | None]] = insert_missing_info(installers_dict=installers_dict_deduped)
    logging.info("Finished retrieving information...")

    logging.info("Retrieving updated installer data from GOG...")
    online_info: dict[str, dict[str, str | bool | None]] = get_online_data(local_info=local_info)
    logging.info("Finished retrieving updated data.")

    logging.info("Comparing installer versions...")
    new_versions_dict: dict[str, dict[str, str | bool]] = compare_versions(local_info=local_info,
                                                                           online_info=online_info)
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


def write_installer_list(new_versions_dict: dict[str, dict[str, str | bool]],
                         output_file: str) -> None:
    dirname = os.path.dirname(output_file)
    name, ext = os.path.splitext(os.path.basename(output_file))

    name = f"{name}_{CURRENT_DATE}"

    output_file = os.path.join(dirname, name + ext)

    try:
        with open(output_file, "w", encoding="utf-8") as file_stream:
            for product_id in new_versions_dict.keys():
                product_name = new_versions_dict[product_id].get("product_name")
                local_version = new_versions_dict[product_id].get("local_version", UNKNOWN)
                local_build = new_versions_dict[product_id].get("local_build", UNKNOWN)
                online_version = new_versions_dict[product_id].get("online_version", UNKNOWN)
                online_build = new_versions_dict[product_id].get("online_build", UNKNOWN)

                local_old_version: bool = new_versions_dict[product_id].get("local_old_version")
                online_old_version: bool = new_versions_dict[product_id].get("online_old_version")

                file_stream.write(f"{product_name} ({product_id})" + "\n")

                if local_old_version and not online_old_version:
                    file_stream.write(f"{local_version} {{OLD GEN INSTALLER}} -> {online_version}" + "\n")
                else:
                    file_stream.write(f"{local_version} -> {online_version}" + "\n")

                file_stream.write(f"{local_build} -> {online_build}" + "\n")
                file_stream.write("\n\n")
    except PermissionError:
        logging.critical("Couldn't open output file.", exc_info=True)
        sys.exit(1)


def get_installers_list(paths: list[str]) -> tuple[str, ...] | None:
    installer_list_aux: list[str] = []
    installers_list: list[str] = []

    logging.info("Retrieving executables from given paths...")

    for path in paths:
        executable_list = glob.glob("**" + os.path.sep + "*.exe", root_dir=path, recursive=True)

        if len(executable_list) == 0:
            logging.info(f"No executables found in \"{path}\".")
            continue

        for item in executable_list:
            installer_list_aux.append(os.path.join(path, item))

    if len(installer_list_aux) == 0:
        logging.error("No executables found in any of the paths.")
        return None

    for item in sorted(installer_list_aux):
        if INSTALLER_REGEX.search(item) is not None:
            installers_list.append(item)

    return tuple(sorted(installers_list))


def map_product_id(installers_list: tuple[str, ...]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for installer in installers_list:
        basename = os.path.basename(installer)

        logging.info(f"Retrieving product ID for \"{basename}\"...")

        product_id: str | None = get_product_id(installer_path=installer,
                                                basename=basename)

        if product_id is not None:
            mapping[installer] = product_id
            logging.info(f"Product ID for \"{basename}\" retrieved successfully.")
            logging.info(f"Product ID: {product_id}")

    return mapping


def get_product_id(installer_path: str,
                   basename: str) -> str | None:
    cmd: tuple[str, str, str] = (INNOEXTRACT_PATH,
                                 "-l",
                                 installer_path)

    try:
        innoextract_output = subprocess.run(cmd, encoding="utf_8", capture_output=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        logging.critical("Couldn't get the product id, there was an error executing innoextract.", exc_info=True)
        sys.exit(1)

    product_id = None

    for line in innoextract_output.splitlines():
        try:
            product_id = PRODUCT_ID_REGEX_1.match(line).groups()[0]
            break
        except (AttributeError, IndexError):
            try:
                product_id = PRODUCT_ID_REGEX_2.match(line).groups()[0]
                break
            except (AttributeError, IndexError):
                continue

    if product_id is not None:
        return product_id

    # try by retrieving the product name from the properties and searching it on GOG

    logging.info("Could not find the product ID with innoextract. Falling to search the product name on GOG and "
                 "retrieve the product ID from the response.")

    exe_info = get_exe_info(installer_path)

    if exe_info is None:
        logging.warning(f"Couldn't get the product ID for \"{basename}\". Please report this.")
        return None

    product_name = exe_info.ProductName

    if product_name is None:
        logging.error(f"Couldn't get the product name & ID for \"{basename}\". Please report this.")
        return None

    if DATA.Delisted_Games is not None:
        if product_name in DATA.Delisted_Games:
            logging.info(f"Delisted game: {product_name}, skipping...")
            return None

    if DATA.Strings_To_Remove is not None:
        for pattern in DATA.Strings_To_Remove:
            product_name = pattern.sub("", product_name)

    if DATA.Replace_Names is not None:
        product_name = DATA.Replace_Names.get(product_name, DATA.Replace_Names.get(exe_info.ProductName, product_name))

    while re.search(r"[a-zá-úñ]-", product_name, flags=re.IGNORECASE):
        product_name = re.sub(r"([a-zá-úñ])-\s", r"\1 - ", product_name, flags=re.IGNORECASE)

    logging.info(f"Performing public search to get the product ID of: {product_name}...")
    return search_product_id_on_gog(product_name=product_name)


def search_product_id_on_gog(product_name: str) -> str | None:
    gog_url = Template("https://embed.gog.com/games/ajax/filtered?mediaType=game&search=$search_query")
    gog_info = download_data(gog_url=gog_url,
                             product_name=product_name)

    if gog_info is None:
        return None

    if gog_info.get("totalGamesFound") == 0:
        found_numeral = False
        if DATA.Roman_Numerals is not None:
            for numeral in DATA.Roman_Numerals:
                if numeral in product_name.split():
                    found_numeral = True
                    product_name = re.sub(f"\b{numeral}\b", str(DATA.Roman_Numerals[numeral]), product_name, 1)

            if found_numeral:
                logging.info(f"Roman number found in \"{product_name}\", replacing with decimal equivalent and "
                             f"searching again...")
                return search_product_id_on_gog(product_name)

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


def get_exe_info(file_path: str) -> ExecutableInfo | None:
    """
    | Available attributes (their value may or may not be Nonetype):

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

    Parameters:
        file_path: Path to executable file.
    Returns:
         Object containing information about the executable file.
    """

    global global_exe_info

    # FIXME: I'm retrieving this information twice, maybe store it in the dict

    logging.info(f"Extracting information from executable file: {os.path.basename(file_path)}")

    if global_exe_info.get(file_path, "") != "":
        logging.info(f"Information for executable file: \"{file_path}\" was found, reusing...")
        return copy.deepcopy(global_exe_info[file_path])

    properties: tuple[str, ...] = ("Comments", "InternalName", "ProductName", "CompanyName", "LegalCopyright",
                                   "ProductVersion", "FileDescription", "LegalTrademarks", "PrivateBuild",
                                   "FileVersion", "OriginalFilename", "SpecialBuild")

    try:
        lang, codepage = win32api.GetFileVersionInfo(file_path, "\\VarFileInfo\\Translation")[0]
        properties_dict: dict[str, str | None] = {}

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

        global_exe_info[file_path] = ExecutableInfo.model_validate(properties_dict)

        return global_exe_info[file_path]
    except:
        logging.error("An error occurred while trying to get the information from the executable:", exc_info=True)
        return None


def dedup_installers_id(installers_dict: dict[str, str]) -> dict[str, dict[str, str]]:
    id_list = set(installers_dict.values())

    deduped_installers_dict_aux: dict[str, dict[str, str]] = {}

    # TODO: Maybe don't do this? So people know they have the same installer in more than one location?
    for product_id in id_list:
        found = False

        while not found:
            for installer_path in sorted(installers_dict.keys()):  # type: str
                if installers_dict[installer_path] == product_id:
                    found = True
                    deduped_installers_dict_aux[installer_path] = {
                        "product_id": product_id
                    }
                    break

    deduped_installers_dict: dict[str, dict[str, str]] = {}

    for item in sorted(deduped_installers_dict_aux.keys()):
        deduped_installers_dict[item] = copy.deepcopy(deduped_installers_dict_aux[item])

    return deduped_installers_dict


def insert_missing_info(installers_dict: dict[str, dict[str, str]]) -> dict[str, dict[str, str | bool | None]]:
    """
    Insert missing installer information into the installers_dict.

    Returns:
         A new dictionary containing the complete installer information
    """

    local_info: dict[str, dict[str, str | bool]] = copy.deepcopy(installers_dict)

    for installer_path in sorted(installers_dict.keys()):
        tmp_dir = tempfile.mkdtemp()

        try:
            basename = os.path.basename(installer_path)

            logging.info(f"Processing \"{basename}\"...")

            if OLD_VERSION_REGEX.search(installer_path) is not None:
                logging.info(f"\"{basename}\" detected as old gen.")
                old_installer = True
            else:
                logging.info(f"\"{basename}\" detected as current gen.")
                old_installer = False

            product_id = installers_dict[installer_path]["product_id"]

            logging.info("Retrieving installer information from file properties...")

            local_info.update(get_local_info_from_exe(file_path=installer_path,
                                                      old_installer=old_installer))
            local_info[installer_path].update({
                "product_id": product_id
            })

            logging.info("Finished retrieving installer information from file properties.")
            logging.info("Starting extraction of info file from installer...")

            info_file: str | None
            if old_installer:
                info_file = extract_info_file_old(product_id=product_id,
                                                  tmp_dir=tmp_dir,
                                                  file_path=installer_path)
            else:
                info_file = extract_info_file(product_id=product_id,
                                              tmp_dir=tmp_dir,
                                              file_path=installer_path)

            if info_file is None:
                continue

            logging.info("Finished extraction of info file.")

            # When using 7-Zip to extract the info file, the file is extracted with the original directory structure
            # which might be a subdirectory,
            # in this case we have to move the info file to the root of the temporary directory.
            move_info_file_to_root(tmp_dir=tmp_dir)

            logging.info("Reading info file...")

            with open(os.path.join(tmp_dir, info_file),
                      mode="r",
                      encoding="utf_8",
                      errors="backslashreplace") as file_stream:
                info_file_content = json.load(file_stream)

            logging.info("Checking for non-base game installer")

            if not is_main_game(installer_info=info_file_content):
                logging.info("Installer is not a base game installer, removing from update checking...")
                local_info.pop(installer_path)
                continue

            logging.info("Filling all the missing info for the installer...")

            if old_installer:
                if local_info[installer_path].get("version_name") is None:
                    logging.info("Trying to retrieve the version from the filename...")

                    version_name = get_old_version_from_filename(filename=os.path.basename(installer_path))
                    if version_name is not None:
                        # If the version_name variable is None, we don't need to do anything as that's the fallback
                        # value when getting the info from the exe properties.
                        local_info[installer_path]["version_name"] = version_name

                if info_file_content.get("name") is not None:
                    # Game's name from the info file is preferred against the ProductName property of the executable
                    # file
                    local_info[installer_path]["product_name"] = info_file_content["name"]
            else:
                if local_info[installer_path].get("build_id") is None:
                    try:
                        local_info[installer_path]["build_id"] = info_file_content["buildId"]
                    except KeyError:
                        logging.info("Build ID not found in info file and file properties.")

                if local_info[installer_path].get("version_name") is None:
                    logging.info("Trying to retrieve the version from the filename...")

                    local_info[installer_path]["version_name"] = get_version_from_filename(filename=basename)

                    if local_info[installer_path].get("version_name") is None:
                        logging.info("Could not get the version from the filename.")

                if info_file_content.get("name") is not None:
                    local_info[installer_path]["product_name"] = info_file_content["name"]

            logging.info("Finished filling missing info for the installer.")
            logging.info(f"Finished processing \"{basename}\".")
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except (FileNotFoundError, PermissionError):
                pass

    return local_info


def get_local_info_from_exe(file_path: str,
                            old_installer: bool) -> dict[str, dict[str, str | bool]]:
    exe_info: ExecutableInfo = get_exe_info(file_path=file_path)

    product_name = exe_info.ProductName

    build_id = None
    if exe_info.ProductVersion is not None:
        try:
            build_id = BUILD_ID_REGEX.search(exe_info.ProductVersion).groups()[0]
        except (AttributeError, IndexError):
            pass

    version_name = None
    if not old_installer:
        if exe_info.ProductVersion is not None:
            try:
                version_name = VERSION_NAME_REGEX.search(exe_info.ProductVersion).groups()[0]
            except (AttributeError, IndexError, KeyError):
                pass
    else:
        version_name = exe_info.ProductVersion

    info_dict: dict[str, dict[str, str | bool]] = {
        file_path: {
            "build_id": build_id,
            "product_name": product_name,
            # old_version refers to the installer, not the game version.
            "old_version": old_installer,
            # If both local and online installers are old versions, we use the version name found in the filename to
            # compare versions, so we have to retrieve and store this.
            "version_name": version_name
        }
    }

    return info_dict


def extract_info_file(product_id: str,
                      tmp_dir: str,
                      file_path: str) -> str | None:
    """
    Extract the info file from the given installer to the specified temporary directory,
    returning the name of the info file.

    Parameters:
        product_id: ID of the product to extract the info from
        tmp_dir: Temporary directory where to extract the info file
        file_path: Installer path

    Returns:
        The name of the info file or *None* if no info file could be extracted
    """

    info_file: str = f"goggame-{product_id}.info"

    cmd_extract: tuple[str, ...] = (INNOEXTRACT_PATH,
                                    "-e",
                                    "-I",
                                    info_file,
                                    "-d",
                                    tmp_dir,
                                    file_path)

    try:
        subprocess.run(cmd_extract, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        logging.error(f"Error extracting info file from {file_path}:", exc_info=True)
        return None

    if len(os.listdir(tmp_dir)) != 0:
        return info_file
    else:
        logging.error("Couldn't extract info file from installer.")
        return None


def extract_info_file_old(product_id: str,
                          tmp_dir: str,
                          file_path: str) -> str | None:
    """
    Extract the info file from the given old gen installer to the specified temporary directory,
    returning the name of the info file.

    Parameters:
        product_id: ID of the product to extract the info from
        tmp_dir: Temporary directory where to extract the info file
        file_path: Installer path

    Returns:
        The name of the info file or *None* if no info file could be extracted
    """

    file_basename = os.path.basename(file_path).replace(".exe", "")
    file_parent_dir = os.path.dirname(file_path)

    bin_list = glob.glob(file_basename + "-*.bin", root_dir=file_parent_dir)

    if len(bin_list) == 0:
        bin_list = glob.glob(file_basename + ".bin", root_dir=file_parent_dir)

    if len(bin_list) != 0:
        info_file = f"goggame-{product_id}.info"
        info_file_in_bin = os.path.join("game", info_file)

        password = md5(product_id.encode()).hexdigest()

        bin_path = os.path.join(file_parent_dir, bin_list[0])

        cmd_extract: tuple[str, ...] = (SEVENZIP_PATH,
                                        "e",
                                        bin_path,
                                        f"-o{tmp_dir}",
                                        info_file_in_bin,
                                        "-aoa",
                                        "-y",
                                        f"-p{password}")

        try:
            subprocess.run(cmd_extract, encoding="utf-8", stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            logging.error(f"Error extracting info file from {file_path}:", exc_info=True)
            return None

        if len(os.listdir(tmp_dir)) != 0:
            return info_file
        else:
            logging.error("Couldn't extract info file from installer.")
            return None
    else:
        logging.info("Installer is not using the old RAR compression, trying with innoextract...")
        return extract_info_file(product_id=product_id,
                                 tmp_dir=tmp_dir,
                                 file_path=file_path)


def move_info_file_to_root(tmp_dir: str) -> None:
    logging.info("Checking if info file is not located in the root of the temporary directory...")

    file_path = glob.glob(f"**{os.path.sep}*.info", root_dir=tmp_dir, recursive=True)[0]
    file_path = os.path.join(tmp_dir, file_path)

    correct_path = os.path.join(tmp_dir, os.path.split(file_path)[1])

    if file_path != correct_path:
        logging.info("Moving info file to the root...")
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

    Parameters:
        installer_info: Installer information

    Returns:
        Whether the installer belongs to a main game
    """
    if installer_info.get("dependencyGameId") is not None:
        if installer_info.get("dependencyGameId") != "":
            return False
        else:
            return True
    elif installer_info.get("gameId") != installer_info.get("rootGameId"):
        return False
    elif DATA.Goodies_ID is not None:
        if str(installer_info.get("rootGameId")) in DATA.Goodies_ID:
            return False
        else:
            return True
    else:
        return True


def get_old_version_from_filename(filename: str):
    """
    Try to extract the version from the filename for old gen installers.

    Parameters:
        filename: Filename where to extract the version from

    Returns:
        Version string if found, else *None*
    """
    try:
        version_name = OLD_VERSION_REGEX.search(filename).groups()[0]
    except (AttributeError, IndexError):
        version_name = None
        logging.error("Couldn't get the version from the filename.")

    return version_name


def get_version_from_filename(filename: str) -> str | None:
    """
    Try to extract the version from the filename.

    Versioning (at least in games) varies wildly, so it's extremely complicated to extract the correct version from
    the filename, which is why this is the last option.

    Parameters:
        filename: Filename where to extract the version from

    Returns:
        Version string if found, else *None*
    """

    filename = os.path.basename(filename)

    try:
        version_name = EXTRACT_VERSION_REGEX.search(filename).groups()[0]

        version_name = version_name.strip().replace("_", " ")

        return version_name
    except (AttributeError, IndexError):
        logging.warning(f"Could not extract version from \"{filename}\"")
        return None


def get_online_data(local_info: dict[str, dict[str, str | bool | None]]) -> dict[str, dict[str, str | bool | None]]:

    online_info: dict[str, dict[str, str | bool | None]] = {}

    for installer in sorted(local_info):
        basename = os.path.basename(installer)

        logging.info(f"Downloading updated data for \"{basename}\"...")

        product_id = local_info[installer]["product_id"]

        load_online_data(product_id=product_id,
                         online_info=online_info,
                         file_path=installer)

        logging.info(f"Finished downloading data for \"{basename}\".")

    return online_info


def load_online_data(product_id: str,
                     online_info: dict[str, dict[str, str | bool | None]],
                     file_path: str) -> None:
    logging.info(f"Retrieving latest build and version for \"{product_id}\"...")

    gog_url: Template = Template("https://content-system.gog.com/products/$search_query/os/windows/builds?generation=2")

    gog_dict: dict | None = download_data(product_id=product_id,
                                          gog_url=gog_url)

    if gog_dict is None:
        online_info[product_id] = {
            "version_name": None,
            "build_id": None,
            "old_version": None
        }
        return

    if gog_dict["count"] == 0:
        logging.info("Trying to find if the product ID belongs to a pack and extract the (actual) game ID...")

        new_product_id: str | None = get_product_id_from_pack(product_id=product_id)

        if new_product_id is not None and new_product_id != product_id:
            gog_dict = download_data(product_id=new_product_id,
                                     gog_url=gog_url)

    if gog_dict["count"] == 0:
        logging.warning(f"Product \"{os.path.basename(file_path)}\" ({product_id}) build information wasn't found on "
                        f"GOG.")
        online_info[product_id] = {
            "version_name": None,
            "build_id": None,
            "old_version": None
        }
        return

    if gog_dict["items"][0].get("legacy_build_id") is None:
        last_version: str = gog_dict["items"][0]["version_name"]
        last_build: str = gog_dict["items"][0]["build_id"]

        online_info[product_id] = {
            "version_name": last_version,
            "build_id": last_build,
            # old_version refers to the online installer version, not the game version
            "old_version": False
        }
    else:
        logging.info("Only old gen installers are available for this game.")

        last_legacy_build_id: str = str(gog_dict["items"][0].get("legacy_build_id"))

        # if the last online installer is old_version, then we have to get the version from the (online) filename, for
        # this we have to download the repository info.
        logging.info("Retrieving latest legacy installer version...")
        last_version: str | None = get_last_version_old_installer(last_legacy_build_id=last_legacy_build_id,
                                                                  product_id=product_id)

        online_info[product_id] = {
            "version_name": last_version,
            "build_id": last_legacy_build_id,
            "old_version": True
        }

    logging.info("Finished retrieving latest build and version.")


def download_data(gog_url: Template,
                  product_id: str = None,
                  legacy_build_id: str = None,
                  product_name: str = None) -> dict | None:
    try:
        if legacy_build_id is not None and product_id is not None:
            logging.info(f"Downloading repository data for \"{product_id}\"...")
            gog_url = gog_url.substitute(search_query=product_id, legacy_build_id=legacy_build_id)
        elif product_name is not None:
            logging.info(f"Downloading search data for \"{product_name}\"...")
            gog_url = gog_url.substitute(search_query=product_name)
        elif product_id is not None:
            logging.info(f"Downloading game data for \"{product_id}\"...")
            gog_url = gog_url.substitute(search_query=product_id)
        else:
            raise ValueError("download_data() needs at least one of product_id or legacy_build_id or product_name.")

        api_response = SESSION.get(gog_url)
        api_response.raise_for_status()
        api_response_data = api_response.content.decode(encoding="utf_8", errors="backslashreplace")

        return json.loads(api_response_data)
    except RequestException:
        logging.error(f"There was an error downloading the data for: {product_id}", exc_info=True)
        return None


def get_product_id_from_pack(product_id: str) -> str | None:
    gog_url = Template("https://api.gog.com/v2/games/$search_query?locale=en-US")

    gog_dict: dict | None = download_data(product_id=product_id,
                                          gog_url=gog_url)

    if gog_dict is None:
        return None

    product_type: str | None
    try:
        product_type = gog_dict.get("_embedded").get("productType")
    except AttributeError:
        product_type = None

    if product_type is None or product_type != "PACK":
        return product_id

    try:
        new_url: str = gog_dict.get("_links").get("includesGames")[0].get("href").strip()
        product_id: str = PRODUCT_ID_FROM_URL_REGEX.search(new_url).groups()[0]
        return product_id
    except (AttributeError, IndexError):
        return None


def get_last_version_old_installer(last_legacy_build_id: str,
                                   product_id: str) -> str | None:
    gog_url: Template = Template(
            "https://cdn.gog.com/content-system/v1/manifests/$search_query/windows/$legacy_build_id/repository.json"
    )

    gog_dict_old: dict | None = download_data(product_id=product_id,
                                              gog_url=gog_url,
                                              legacy_build_id=last_legacy_build_id)

    if gog_dict_old is None:
        return None

    try:
        online_filename = gog_dict_old["product"]["support_commands"][0]["executable"]
    except AttributeError:
        return None

    try:
        online_version = OLD_VERSION_REGEX.search(online_filename).groups()[0]
    except (AttributeError, IndexError):
        online_version = None

    return online_version


def compare_versions(local_info: dict[str, dict[str, str | bool | None]],
                     online_info: dict[str, dict[str, str | bool | None]]) -> dict[str, dict[str, str | bool]]:
    new_versions_dict: dict[str, dict[str, str | bool]] = {}
    local_info = sort_local_info(local_info=local_info)

    print("")  # to add space
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

    return new_versions_dict


def sort_local_info(local_info: dict[str, dict[str, str | bool]]) -> dict[str, dict[str, str | bool]]:
    """
    Sort installers by product_name

    Returns:
        Sorted installers
    """
    logging.info("Sorting local information by product name...")

    sorted_local_info = {}
    installer_name_map = {}

    for installer in sorted(local_info):
        product_name = local_info[installer]["product_name"]
        installer_name_map[product_name] = installer

    for prod_name in sorted(installer_name_map):
        installer = installer_name_map[prod_name]

        sorted_local_info.update({
            installer: local_info[installer]
        })

    return copy.deepcopy(sorted_local_info)


def compare_new_versions(local_installer_info: dict[str, str | bool | None],
                         online_info: dict[str, dict[str, str | bool | None]],
                         new_versions_dict: dict[str, dict[str, str | bool]]) -> None:
    """
    Compare the local installer information and the online installer information for current gen installers.

    Parameters:
        local_installer_info: Local installer information
        online_info: Online installer information
        new_versions_dict: Dictionary where to store new versions
    """
    product_id: str = local_installer_info["product_id"]
    product_name: str = local_installer_info["product_name"]

    online_version: str = online_info[product_id]["version_name"]
    local_version: str | None = local_installer_info["version_name"]

    online_build: int | str
    try:
        online_build = int(online_info[product_id]["build_id"])
    except TypeError:
        online_build = UNKNOWN

    local_build: int | str
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

            new_versions_dict[product_id] = {
                "product_name": product_name,
                "local_version": local_version,
                "local_build": local_build,
                "local_old_version": False,
                "online_version": online_version,
                "online_build": online_build,
                "online_old_version": False
            }

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

        local_version_norm: str = normalize_version_name(local_version)
        online_version_norm: str = normalize_version_name(online_version)

        if local_version_norm.lower() != online_version_norm.lower():
            logging.info("Online version is newer than local version, updated installer found!")

            new_versions_dict[product_id] = {
                "product_name": product_name,
                "local_version": local_version,
                "local_build": local_build,
                "local_old_version": False,
                "online_version": online_version,
                "online_build": online_build,
                "online_old_version": False
            }

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

    Returns:
        True if both versions are found, else False
    """
    if DATA.Match_Versions is None or len(DATA.Match_Versions) == 0:
        return False

    if product_id in DATA.Match_Versions:
        for version_list in DATA.Match_Versions[product_id]:
            if online_version in version_list and local_version in version_list:
                logging.info(f"Local ({local_version}) and online ({online_version}) versions found in"
                             f"\"Match_Versions\" on the data file for \"{product_id}\", they will be assumed to be "
                             f"the same.")
                return True

    return False


def normalize_version_name(version_name: str) -> str:
    # This was done for the cases when the version has to be extracted from the filename where the online version might
    # have some of these characters, but they are illegal in Windows so the extracted version won't have them
    illegal_characters: tuple[str, ...] = ("#", "!", "?", "\\", "/", "~", "|", "&", "$")

    new_version_name: str = version_name

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
                               product_id: str) -> str | None:
    """
    Try to get the version of the local installer directly from GOG.

    Parameters:
        local_build: Build ID of the local installer
        product_id: Product ID of the local installer

    Returns:
        Version of the local installer or *None* if no version could be found
    """

    gog_url: Template = Template("https://content-system.gog.com/products/$search_query/os/windows/builds?generation=2")

    gog_dict: dict | None = download_data(product_id=product_id,
                                          gog_url=gog_url)

    if gog_dict is None:
        return None

    for item in gog_dict["items"]:
        if item["build_id"] == local_build:
            return item["version_name"]

    return None


def compare_old_versions(local_installer_info: dict[str, str | bool | None],
                         online_info: dict[str, dict[str, str | bool | None]],
                         new_versions_dict: dict[str, dict[str, str | bool]]) -> None:
    """
    Compare the local installer information -which is old gen- and the online installer information that might or might
    not be old gen.

    Parameters:
        local_installer_info: Local installer information
        online_info: Online installer information
        new_versions_dict: Dictionary where to store new versions
    """
    product_id: str = local_installer_info["product_id"]
    product_name: str = local_installer_info["product_name"]

    if online_info[product_id].get("old_version") is None:
        logging.info(f"Product ID \"{product_id}\" wasn't found online, nothing to compare. Skipping...")
        return

    online_version: str | None = online_info[product_id]["version_name"]
    local_version: str | None = local_installer_info["version_name"]

    if online_version is None:
        online_version = UNKNOWN

    if local_version is None:
        local_version = UNKNOWN

    local_build: str | None = local_installer_info["build_id"]
    online_build: str | None = online_info[product_id]["build_id"]

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

            new_versions_dict[product_id] = {
                "product_name": product_name,
                "local_version": local_version,
                "local_build": local_build,
                "local_old_version": True,
                "online_version": online_version,
                "online_build": online_build,
                "online_old_version": True
            }

            print(f"{product_name} ({product_id}) : {local_version} -> {online_version}")
        else:
            logging.info("Online version is either older (which is highly unlikely) or same version as local.")
    else:
        logging.info("Online installer is current gen and local installer is old gen. Assuming an update "
                     "is available.")

        new_versions_dict[product_id] = {
            "product_name": product_name,
            "local_version": local_version,
            "local_build": local_build,
            "local_old_version": True,
            "online_version": online_version,
            "online_build": online_build,
            "online_old_version": False
        }

        print(f"{product_name} ({product_id}) : {local_version} {{OLD GEN INSTALLER}} -> {online_version}")


if __name__ == '__main__':
    main()
