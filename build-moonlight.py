#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# ============================================================
# Configuration
# ============================================================

ROOT = Path(__file__).resolve().parent
VSCODE_DIR = ROOT / ".build" / "vscode"

UPSTREAM_URL = "https://github.com/microsoft/vscode.git"

# Pin this to the VS Code revision you want to build.
#
# You can use a release tag:
#     1.104.0
#
# or a specific commit:
#     abcdef123456...
#
UPSTREAM_REF = "main"

PRODUCT_CONFIG = ROOT / "config" / "product.json"
BRANDING_DIR = ROOT / "config" / "branding"
EXTENSIONS_CONFIG = ROOT / "config" / "extensions.json"
EXTENSION_UPDATER = ROOT / "scripts" / "update-extensions.py"


# ============================================================
# Output
# ============================================================

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    BLUE = "\033[34m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"


def log(message):
    print(f"{Colors.CYAN}[moonlight]{Colors.RESET} {message}")


def step(number, total, message):
    print()
    print(
        f"{Colors.BOLD}"
        f"[{number}/{total}]"
        f"{Colors.RESET} "
        f"{message}"
    )


def success(message):
    print(f"{Colors.GREEN}[moonlight] {message}{Colors.RESET}")


def warning(message):
    print(f"{Colors.YELLOW}[moonlight] WARNING: {message}{Colors.RESET}")


def error(message):
    print(f"{Colors.RED}[moonlight] ERROR: {message}{Colors.RESET}")


# ============================================================
# Helpers
# ============================================================

def run(command, cwd=None):
    """Run a command and abort if it fails."""

    log(f"$ {' '.join(map(str, command))}")

    try:
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        error(
            f"Command failed with exit code {exc.returncode}: "
            f"{' '.join(map(str, command))}"
        )
        sys.exit(exc.returncode)


def command_exists(command):
    return shutil.which(command) is not None


def require_command(command):
    if not command_exists(command):
        error(f"Required command '{command}' was not found in PATH.")
        sys.exit(1)


def load_json(path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        error(f"Missing configuration file: {path}")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        error(f"Invalid JSON in {path}: {exc}")
        sys.exit(1)


# ============================================================
# VS Code source
# ============================================================

def clone_vscode():
    if VSCODE_DIR.exists():
        return

    log(f"Cloning VS Code into {VSCODE_DIR}")

    VSCODE_DIR.parent.mkdir(parents=True, exist_ok=True)

    run([
        "git",
        "clone",
        "--filter=blob:none",
        UPSTREAM_URL,
        str(VSCODE_DIR),
    ])


def update_vscode():
    log(f"Updating VS Code to '{UPSTREAM_REF}'")

    run(["git", "fetch", "--tags", "origin"], cwd=VSCODE_DIR)

    # Throw away modifications inside the generated checkout.
    #
    # This is intentional: all Moonlight modifications should come
    # from this repository's config/patches rather than being made
    # manually inside .build/vscode.
    run(["git", "reset", "--hard"], cwd=VSCODE_DIR)
    run(["git", "clean", "-fd"], cwd=VSCODE_DIR)

    run(["git", "checkout", "--force", UPSTREAM_REF], cwd=VSCODE_DIR)

    # If UPSTREAM_REF is a branch, update it.
    result = subprocess.run(
        [
            "git",
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/remotes/origin/{UPSTREAM_REF}",
        ],
        cwd=VSCODE_DIR,
    )

    if result.returncode == 0:
        run(
            [
                "git",
                "reset",
                "--hard",
                f"origin/{UPSTREAM_REF}",
            ],
            cwd=VSCODE_DIR,
        )

    commit = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=VSCODE_DIR,
        text=True,
    ).strip()

    success(f"Using VS Code revision {commit}")


# ============================================================
# product.json
# ============================================================

def merge_dict(base, overrides):
    """
    Recursively merge overrides into base.

    Dictionaries are merged recursively.
    Everything else is replaced by the override.
    """

    for key, value in overrides.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            merge_dict(base[key], value)
        else:
            base[key] = value


def apply_product_config():
    if not PRODUCT_CONFIG.exists():
        warning("No config/product.json found; skipping product configuration.")
        return

    product_path = VSCODE_DIR / "product.json"

    if not product_path.exists():
        error(f"VS Code product.json was not found at {product_path}")
        sys.exit(1)

    log("Applying Moonlight product configuration")

    base = load_json(product_path)
    overrides = load_json(PRODUCT_CONFIG)

    merge_dict(base, overrides)

    with product_path.open("w", encoding="utf-8") as f:
        json.dump(base, f, indent=4, ensure_ascii=False)
        f.write("\n")

    success("product.json updated")


# ============================================================
# Branding
# ============================================================

def apply_branding():
    if not BRANDING_DIR.exists():
        warning("No config/branding directory found; skipping branding.")
        return

    log("Applying Moonlight branding")

    # The branding directory mirrors paths inside the VS Code tree.
    #
    # Example:
    #
    # config/branding/resources/linux/code.png
    #
    # becomes:
    #
    # .build/vscode/resources/linux/code.png
    #
    for source in BRANDING_DIR.rglob("*"):
        if source.is_dir():
            continue

        relative = source.relative_to(BRANDING_DIR)
        destination = VSCODE_DIR / relative

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

        log(f"  {relative}")

    success("Branding applied")


# ============================================================
# Extensions
# ============================================================

def update_extensions():
    if not EXTENSION_UPDATER.exists():
        warning(
            f"Extension updater not found at {EXTENSION_UPDATER}; "
            "skipping extension update."
        )
        return

    log("Updating Moonlight extensions")

    command = [
        sys.executable,
        str(EXTENSION_UPDATER),
    ]

    if EXTENSIONS_CONFIG.exists():
        command.append(str(EXTENSIONS_CONFIG))

    run(command, cwd=ROOT)

    success("Extensions updated")


# ============================================================
# Dependencies
# ============================================================

def run_with_nvm(command, cwd):
    """
    Run a command using the Node version specified by .nvmrc.

    A temporary Bash script is used so the NVM environment is
    initialized normally inside Bash.
    """

    nvmrc = Path(cwd) / ".nvmrc"

    if not nvmrc.exists():
        error(f"No .nvmrc found in {cwd}")
        sys.exit(1)

    command_string = " ".join(
        subprocess.list2cmdline([str(arg)])
        for arg in command
    )

    script = f"""\
#!/usr/bin/env bash
set -e

export NVM_DIR="$HOME/.nvm"

if [ ! -s "$NVM_DIR/nvm.sh" ]; then
    echo "ERROR: NVM could not be found at $NVM_DIR/nvm.sh"
    exit 1
fi

\. "$NVM_DIR/nvm.sh"

nvm install
nvm use

{command_string}
"""

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".sh",
        prefix="moonlight-",
        delete=False,
    ) as f:
        f.write(script)
        script_path = Path(f.name)

    try:
        script_path.chmod(0o755)

        run(
            ["bash", "-c", str(script_path)],
            cwd=cwd,
        )
    finally:
        script_path.unlink(missing_ok=True)

def install_dependencies():
    log("Installing VS Code dependencies")

    run_with_nvm(
        ["npm", "install"],
        VSCODE_DIR,
    )

    success("Dependencies installed")
    
def build_vscode():
    log("Building Moonlight")

    run_with_nvm(
        [
            "npm",
            "run",
            "gulp",
            "vscode-linux-x64",
        ],
        VSCODE_DIR,
    )

    success("Moonlight build completed")

# ============================================================
# Build
# ============================================================

def build_vscode():
    log("Building Moonlight")

    run_with_nvm(
        [
            "npm",
            "run",
            "gulp",
            "vscode-linux-x64",
        ],
        VSCODE_DIR,
    )

    success("Moonlight build completed")


# ============================================================
# Clean
# ============================================================

def clean():
    build_dir = ROOT / ".build"

    if not build_dir.exists():
        log("Nothing to clean.")
        return

    log(f"Removing {build_dir}")

    shutil.rmtree(build_dir)

    success("Build directory removed")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Build Moonlight from upstream VS Code."
    )

    parser.add_argument(
        "--update",
        action="store_true",
        help="Fetch and reset the VS Code checkout to UPSTREAM_REF.",
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove the generated .build directory.",
    )

    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Run preparation steps but don't build VS Code.",
    )

    parser.add_argument(
        "--skip-extensions",
        action="store_true",
        help="Build VS Code without updating extensions.",
    )

    args = parser.parse_args()

    print()
    print(
        f"{Colors.BOLD}"
        f"Moonlight Build System"
        f"{Colors.RESET}"
    )
    print()

    require_command("git")

    if args.clean:
        clean()

        if not args.update and not VSCODE_DIR.exists():
            return

    # --------------------------------------------------------
    # 1. Prepare source
    # --------------------------------------------------------

    step(1, 7, "Preparing VS Code source")

    clone_vscode()

    # Always reset when --update is supplied.
    #
    # Without --update, an existing checkout is left alone.
    if args.update:
        update_vscode()
    else:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=VSCODE_DIR,
            text=True,
        ).strip()

        log(f"Using existing VS Code revision {commit}")

    # --------------------------------------------------------
    # 2. Product configuration
    # --------------------------------------------------------

    step(2, 7, "Applying product configuration")

    apply_product_config()

    # --------------------------------------------------------
    # 3. Branding
    # --------------------------------------------------------

    step(3, 7, "Applying branding")

    apply_branding()

    # --------------------------------------------------------
    # 4. Extensions
    # --------------------------------------------------------

    if args.skip_extensions:
        step(4, 7, "Skipping extension update")
        warning("Extension update skipped because --skip-extensions was supplied.")
    else:
        step(4, 7, "Installing Moonlight extensions")
        update_extensions()

    # --------------------------------------------------------
    # 5. Dependencies
    # --------------------------------------------------------

    step(5, 7, "Installing dependencies")

    install_dependencies()

    # --------------------------------------------------------
    # 6. Build
    # --------------------------------------------------------

    if args.skip_build:
        step(6, 7, "Skipping build")
        warning("Build skipped because --skip-build was supplied.")
    else:
        step(6, 7, "Building Moonlight")
        build_vscode()

    # --------------------------------------------------------
    # 7. Done
    # --------------------------------------------------------

    step(7, 7, "Finished")

    if not args.skip_build:
        success("Moonlight is ready.")

        print()
        print(
            f"{Colors.DIM}"
            f"Build output should be inside:"
            f"{Colors.RESET}"
        )
        print(f"  {VSCODE_DIR / '.build'}")
    else:
        success("Moonlight source prepared.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        error("Interrupted.")
        sys.exit(130)