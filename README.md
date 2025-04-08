# Check GOG offline installers for updated versions

Python version required is 3.10+.

Dependencies
-------------------------

- [requests](https://github.com/psf/requests)
- [pywin32](https://github.com/mhammond/pywin32)
- [pydantic](https://github.com/pydantic/pydantic)
- [innoextract](https://github.com/dscharrer/innoextract)
- [7-zip](https://www.7-zip.org)

Usage
-------------------------

```shell
usage: gog-installer-update-checker [-h] [-v] --path PATH [PATH ...]
                                    [--innoextract-path INNOEXTRACT_PATH]
                                    [--seven-zip-path SEVEN_ZIP_PATH]
                                    [--output-file OUTPUT_FILE]
                                    [--data-file DATA_FILE]
                                    [--log-level {debug,info,warning,error}]
                                    [--log-file LOG_FILE]

Check GOG installer for updates

options:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  --path PATH [PATH ...]
                        Path(s) to directories containing GOG installers.
  --innoextract-path INNOEXTRACT_PATH
                        Path to the innoextract executable. By default taken
                        from PATH.
  --seven-zip-path SEVEN_ZIP_PATH
                        Path to the 7-zip executable. By default taken from
                        PATH.
  --output-file OUTPUT_FILE
                        Path to the file where the installers with found
                        updates will be listed. Current date is appended to
                        the name. Default is no output file.
  --data-file DATA_FILE
                        Path to the data file. By default data.json in the app
                        directory is loaded if found, otherwise nothing.
  --log-level {debug,info,warning,error}
                        How much stuff is logged. Can be 'debug', 'info',
                        'warning', 'error'.
  --log-file LOG_FILE   Where to store the log file. Default:
                        'gog_installer_update_checker_{CURRENT_DATE}.log' in
                        the current working directory.
```

- Multiple paths can be specified.
- The app search for innoextract and 7z in PATH if they are not specified. They are required for this program to
  function.
- If no output file is specified, installers that have updates won't be stored and the only place where they will be
  available will be the console output.
- The data file is not required, but without it, things won't go as good.

Datafile Content
-------------------------

The data file is used to store non-critical information. If no data file is specified & found, the program should still
work. The data file may contain the following data:

- [Match_Versions](#match_versions)
- [Replace_Names](#replace_names)
- [Strings_To_Remove](#strings_to_remove)
- [Roman_Numerals](#roman_numerals)
- [Goodies_ID](#goodies_id)
- [Delisted_Games](#delisted_games)

### Match_Versions

Used to force two versions of a specific game to be considered the same. The data type is a `dict` containing a `list`
of `tuple`s with two strings.  
The structure is:

```json
{
  "%PRODUCT_ID%": [
    [
      "%VERSION_A%",
      "%VERSION_B%"
    ],
    [
      "%VERSION_C%",
      "%VERSION_D%"
    ]
  ]
}
```

Where if either the local or online versions of `%PRODUCT_ID%` match any of the two listed strings they will be
considered to be the same version. The versions should either match `"%VERSION_A%"` and `"%VERSION_B%"`
or `"%VERSION_C%"` and `"%VERSION_D%"`

### Replace_Names

Used to replace game titles (called "name" in code). This is mainly for old installers as most of them don't include
information about them inside, and we have to rely on searching for the game title on GOG and use the first result to
get the game ID. The data type is a simple `dict`.  
The structure is:

```json
{
  "%WRONG_NAME%": "%CORRECT_NAME%",
  "%ANOTHER_WRONG_NAME%": "%ANOTHER_CORRECT_NAME%"
}
```

The title of these old installers is taken from the executable property `ProductName`/`Product name`, so if you want to
contribute more titles, you should take the "wrong title" from there.

### Strings_To_Remove

Used to remove parts from the game's title. The data type is a `list` of regular expressions. The two expressions that
come with the default `data.json` file are used like this:

- `\\s+\\([a-z]+\\)$`: To remove languages from the title, like ` (Spanish)`. Notice it must have a whitespace before
  the parenthesis and it must be the last part of the title.
- `\s+[0-9]+th\sAnniversary\sEdition`: To remove `Anniversary Edition` phrases from the title. Notice it must be
  preceded by at least one number and ` th`.

### Roman_Numerals

Used to replace roman numbers from the game titles with decimal numbers. The data type is a simple `dict` where
the `key` is the roman number and `value` is the decimal equivalent.

### Goodies_ID

Used to skip goodies installers as those don't have their information independently published. The data type is a
simple `dict` where the `key` is the product ID and the `value` is the title. The title can be taken from the executable
property `ProductName`/`Product name`.

### Delisted_Games

Used to skip the delisted games from GOG while retrieving the product ID (and processing the installer). Mainly used for
old installers as those don't have their product ID (or actually any information about the installer) embedded, so it
would not be possible to retrieve the ID by doing a public search for the game's title.  
The data type is a simple `list` of game titles, the titles must be taken from the executable's properties.
