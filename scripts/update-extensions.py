#!/usr/bin/env python3

import json
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import platform
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "scripts" / "extensions.json"

MARKETPLACE_API = (
    "https://marketplace.visualstudio.com"
    "/_apis/public/gallery/extensionquery"
    "?api-version=7.2-preview.1"
)

VSIX_ASSET = (
    "Microsoft.VisualStudio.Services.VSIXPackage"
)

USER_AGENT = "VSCode-Fork-Extension-Updater/1.0"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message):
    print(f"[extensions] {message}")


def fail(message):
    print(
        f"[extensions] ERROR: {message}",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------


def get_target_platform():
    system = platform.system()
    machine = platform.machine()

    if system == "Linux":
        if machine == "x86_64":
            return "linux-x64"
        if machine == "aarch64":
            return "linux-arm64"
        if machine == "armv7l":
            return "linux-armhf"

    if system == "Darwin":
        if machine == "x86_64":
            return "darwin-x64"
        if machine == "arm64":
            return "darwin-arm64"

    if system == "Windows":
        if machine == "AMD64":
            return "win32-x64-archive"

    raise RuntimeError(
        f"Unsupported platform: {system} {machine}"
    )

TARGET_PLATFORM = get_target_platform()

# ---------------------------------------------------------------------------
# Microsoft Marketplace
# ---------------------------------------------------------------------------

def query_marketplace(
    extension_id,
    target_platform=None,
):
    criteria = [
        {
            # 7 = extension name (publisher.name)
            "filterType": 7,
            "value": extension_id,
        }
    ]

    if target_platform:
        criteria.append(
            {
                # 23 = target platform
                "filterType": 23,
                "value": target_platform,
            }
        )

    payload = {
        "filters": [
            {
                "criteria": criteria,
                "pageNumber": 1,
                "pageSize": 1,
            }
        ],
        "assetTypes": [
            VSIX_ASSET,
        ],
        "flags": 1 | 2 | 512,
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        MARKETPLACE_API,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    except urllib.error.HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Marketplace API returned HTTP "
            f"{error.code}:\n{body}"
        ) from error


def get_marketplace_vsix(extension_id):
    """
    Find the latest VSIX for the requested platform.

    Marketplace can return multiple entries for the same version,
    one per target platform. Do not assume versions[0] is the
    platform we want.
    """

    log(
        f"Looking for {TARGET_PLATFORM} package"
    )

    result = query_marketplace(
        extension_id,
        None,
    )

    results = result.get(
        "results",
        [],
    )

    if not results:
        raise RuntimeError(
            f"No Marketplace results for "
            f"{extension_id}"
        )

    extensions = results[0].get(
        "extensions",
        [],
    )

    if not extensions:
        raise RuntimeError(
            f"Extension {extension_id} "
            "was not found on the "
            "Microsoft Marketplace"
        )

    extension = extensions[0]

    versions = extension.get(
        "versions",
        [],
    )

    if not versions:
        raise RuntimeError(
            f"No versions returned for "
            f"{extension_id}"
        )

    # ---------------------------------------------------------------
    # First: find the newest version that explicitly targets
    # our platform.
    # ---------------------------------------------------------------

    platform_version = None

    for version in versions:
        if (
            version.get("targetPlatform")
            == TARGET_PLATFORM
        ):
            platform_version = version
            break

    # ---------------------------------------------------------------
    # Second: if there is no platform-specific package, find the
    # universal package.
    #
    # Universal extensions don't have targetPlatform.
    # ---------------------------------------------------------------

    if platform_version is None:
        log(
            f"No {TARGET_PLATFORM} package found, "
            "trying universal package"
        )

        for version in versions:
            if not version.get(
                "targetPlatform"
            ):
                platform_version = version
                break

    # ---------------------------------------------------------------
    # Nothing suitable found.
    # ---------------------------------------------------------------

    if platform_version is None:
        available = sorted(
            {
                v.get("targetPlatform")
                for v in versions
                if v.get("targetPlatform")
            }
        )

        raise RuntimeError(
            f"Extension {extension_id} has no "
            f"{TARGET_PLATFORM} or universal package.\n"
            f"Available platforms: "
            f"{', '.join(available)}"
        )

    version_number = platform_version.get(
        "version",
        "unknown",
    )

    selected_platform = platform_version.get(
        "targetPlatform"
    )

    if selected_platform:
        log(
            f"Selected platform package: "
            f"{selected_platform}"
        )
    else:
        log(
            "Selected universal package"
        )

    # ---------------------------------------------------------------
    # Find VSIX asset
    # ---------------------------------------------------------------

    for asset in platform_version.get(
        "files",
        [],
    ):
        if (
            asset.get("assetType")
            == VSIX_ASSET
        ):
            source = asset.get(
                "source"
            )

            if not source:
                raise RuntimeError(
                    f"VSIX asset for "
                    f"{extension_id} has no "
                    "download URL"
                )

            # -------------------------------------------------------
            # IMPORTANT:
            #
            # The asset URL itself is shared between target platforms.
            # VS Code appends ?targetPlatform=... when downloading it.
            # -------------------------------------------------------

            if selected_platform:
                separator = (
                    "&"
                    if "?" in source
                    else "?"
                )

                source = (
                    f"{source}"
                    f"{separator}"
                    f"targetPlatform="
                    f"{selected_platform}"
                )

            return (
                version_number,
                source,
            )

    raise RuntimeError(
        f"No VSIX asset found for "
        f"{extension_id}"
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download(url, destination):
    log("Downloading:")
    log(f"  {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request) as response:
            status = response.status
            content_type = response.headers.get(
                "Content-Type",
                "",
            )

            log(f"  HTTP {status}")
            log(f"  Content-Type: {content_type}")

            with open(
                destination,
                "wb",
            ) as file:
                shutil.copyfileobj(
                    response,
                    file,
                )

    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"HTTP {error.code} while downloading "
            f"{url}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Failed to download {url}: "
            f"{error.reason}"
        ) from error

    if not zipfile.is_zipfile(destination):
        size = destination.stat().st_size

        raise RuntimeError(
            "Downloaded file is not a valid "
            f"VSIX/ZIP archive "
            f"({size} bytes)"
        )


# ---------------------------------------------------------------------------
# VSIX extraction
# ---------------------------------------------------------------------------

def extract_vsix(vsix_path, destination):
    """
    Extract a VSIX while preserving Unix file permissions.

    Python's ZipFile.extractall() does not reliably restore
    executable permissions from ZIP archives, which matters for
    extensions such as Luau-LSP that ship native binaries.
    """

    log("Extracting VSIX")

    try:
        with zipfile.ZipFile(vsix_path) as archive:
            archive.extractall(destination)

            for info in archive.infolist():
                # Unix permissions are stored in the upper 16 bits.
                permissions = (
                    info.external_attr >> 16
                ) & 0o777

                if permissions == 0:
                    continue

                extracted_path = (
                    destination / info.filename
                )

                if extracted_path.is_file():
                    extracted_path.chmod(
                        permissions
                    )

    except zipfile.BadZipFile as error:
        raise RuntimeError(
            "Downloaded file is not a valid "
            "VSIX/ZIP archive"
        ) from error


# ---------------------------------------------------------------------------
# package.json
# ---------------------------------------------------------------------------

def load_package_json(extension_dir):
    package_path = (
        extension_dir / "package.json"
    )

    if not package_path.exists():
        raise RuntimeError(
            "VSIX does not contain "
            "extension/package.json"
        )

    try:
        with package_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid package.json: {error}"
        ) from error


def save_package_json(
    extension_dir,
    package,
):
    package_path = (
        extension_dir / "package.json"
    )

    with package_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            package,
            file,
            indent=2,
            ensure_ascii=False,
        )

        file.write("\n")


def verify_extension_id(
    package,
    expected_id,
):
    publisher = package.get(
        "publisher"
    )

    name = package.get(
        "name"
    )

    if not publisher or not name:
        raise RuntimeError(
            "Extension package.json does not "
            "contain publisher/name"
        )

    actual_id = (
        f"{publisher}.{name}"
    )

    if actual_id.lower() != expected_id.lower():
        raise RuntimeError(
            "Extension ID mismatch:\n"
            f"  expected: {expected_id}\n"
            f"  received: {actual_id}"
        )


# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------

def find_setting(
    package,
    setting_id,
):
    """
    Find a configuration property in an extension manifest.

    VS Code permits contributes.configuration to be either
    an object or an array.
    """

    configuration = (
        package
        .get("contributes", {})
        .get("configuration", {})
    )

    # Example:
    #
    # "configuration": {
    #     "properties": {
    #         "foo.bar": {}
    #     }
    # }
    if isinstance(
        configuration,
        dict,
    ):
        properties = configuration.get(
            "properties",
            {},
        )

        if setting_id in properties:
            return properties[setting_id]

    # Example:
    #
    # "configuration": [
    #     {
    #         "properties": {
    #             "foo.bar": {}
    #         }
    #     }
    # ]
    elif isinstance(
        configuration,
        list,
    ):
        for contribution in configuration:
            properties = contribution.get(
                "properties",
                {},
            )

            if setting_id in properties:
                return properties[setting_id]

    raise RuntimeError(
        f"Setting not found in extension "
        f"manifest: {setting_id}"
    )


def apply_defaults(
    package,
    defaults,
):
    if not defaults:
        return

    log("Applying default settings")

    for setting_id, value in defaults.items():
        setting = find_setting(
            package,
            setting_id,
        )

        old_value = setting.get(
            "default",
            "<not defined>",
        )

        setting["default"] = value

        log(
            f"  {setting_id}: "
            f"{old_value!r} -> {value!r}"
        )


# ---------------------------------------------------------------------------
# configurationDefaults
# ---------------------------------------------------------------------------

def apply_configuration_defaults(
    package,
    configuration_defaults,
):
    """
    Inject values into:

        contributes.configurationDefaults

    This is useful for things like:

        "[luau]": {
            "editor.defaultFormatter":
                "JohnnyMorganz.stylua"
        }
    """

    if not configuration_defaults:
        return

    contributes = package.setdefault(
        "contributes",
        {},
    )

    existing = contributes.setdefault(
        "configurationDefaults",
        {},
    )

    existing.update(
        configuration_defaults
    )

    log(
        "Applied configuration defaults"
    )


# ---------------------------------------------------------------------------
# Extension update
# ---------------------------------------------------------------------------

def update_extension(extension):
    extension_id = extension["id"]

    destination = (
        ROOT / extension["destination"]
    )

    defaults = extension.get(
        "defaults",
        {},
    )

    configuration_defaults = (
        extension.get(
            "configurationDefaults",
            {},
        )
    )

    log(
        f"Updating {extension_id}"
    )

    # ---------------------------------------------------------------
    # Find latest Marketplace package
    # ---------------------------------------------------------------

    version, download_url = (
        get_marketplace_vsix(
            extension_id
        )
    )

    log(
        f"Marketplace version: {version}"
    )

    # ---------------------------------------------------------------
    # Temporary workspace
    # ---------------------------------------------------------------

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)

        vsix_path = (
            temp / "extension.vsix"
        )

        extracted = (
            temp / "extracted"
        )

        # -----------------------------------------------------------
        # Download
        # -----------------------------------------------------------

        download(
            download_url,
            vsix_path,
        )

        # -----------------------------------------------------------
        # Extract
        # -----------------------------------------------------------

        extract_vsix(
            vsix_path,
            extracted,
        )

        extension_dir = (
            extracted / "extension"
        )

        if not extension_dir.is_dir():
            raise RuntimeError(
                "Invalid VSIX: missing "
                "extension/ directory"
            )

        # -----------------------------------------------------------
        # Read package.json
        # -----------------------------------------------------------

        package = load_package_json(
            extension_dir
        )

        verify_extension_id(
            package,
            extension_id,
        )

        actual_version = package.get(
            "version",
            "unknown",
        )

        log(
            f"Package version: "
            f"{actual_version}"
        )

        # -----------------------------------------------------------
        # Apply custom defaults
        # -----------------------------------------------------------

        apply_defaults(
            package,
            defaults,
        )

        apply_configuration_defaults(
            package,
            configuration_defaults,
        )

        save_package_json(
            extension_dir,
            package,
        )

        # -----------------------------------------------------------
        # Install
        # -----------------------------------------------------------

        # Only delete the old extension after
        # everything above has succeeded.
        if destination.exists():
            log(
                f"Removing old extension: "
                f"{destination}"
            )

            shutil.rmtree(
                destination
            )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copytree(
            extension_dir,
            destination,
        )

        package_json = destination / "package.json"

        if package_json.exists():
            log(
                f"Installing dependencies for "
                f"{extension_id}"
            )

            subprocess.run(
                ["npm", "install", "--omit=dev"],
                cwd=destination,
                check=True,
            )
    log(
        f"Installed {extension_id} "
        f"({actual_version})"
    )

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not CONFIG_PATH.exists():
        fail(
            "Configuration file not found: "
            f"{CONFIG_PATH}"
        )

    try:
        with CONFIG_PATH.open(
            "r",
            encoding="utf-8",
        ) as file:
            config = json.load(file)

    except json.JSONDecodeError as error:
        fail(
            f"Invalid extensions.json: "
            f"{error}"
        )

    extensions = config.get(
        "extensions",
        [],
    )

    if not extensions:
        log(
            "No extensions configured."
        )
        return

    for extension in extensions:
        if "id" not in extension:
            fail(
                "Extension entry is missing "
                "'id'"
            )

        if "destination" not in extension:
            fail(
                f"{extension['id']} is missing "
                "'destination'"
            )

        try:
            update_extension(
                extension
            )

        except Exception as error:
            print(
                "[extensions] ERROR while "
                f"updating {extension.get('id', '<unknown>')}:",
                file=sys.stderr,
            )

            print(
                f"[extensions] {error}",
                file=sys.stderr,
            )

            sys.exit(1)

    log(
        f"Updated {len(extensions)} "
        f"extension(s)."
    )


if __name__ == "__main__":
    main()
