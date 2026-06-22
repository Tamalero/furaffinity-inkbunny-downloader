#!/usr/bin/env fish

set -l script_dir (dirname (realpath (status filename)))
set -l venv_dir   "$script_dir/venv"
set -l marker     "$venv_dir/.deps_installed"

cd $script_dir

# ── Create virtual environment if it doesn't exist ────────────────────────────
if not test -d $venv_dir
    echo "[ setup ] Creating virtual environment…"
    python -m venv $venv_dir
    or begin
        echo "[ error ] Failed to create virtual environment."
        exit 1
    end
end

# ── Activate ──────────────────────────────────────────────────────────────────
source "$venv_dir/bin/activate.fish"

# ── Install / sync dependencies when requirements.txt is newer than marker ───
if not test -f $marker; or test requirements.txt -nt $marker
    echo "[ setup ] Installing / updating dependencies…"
    pip install --quiet -r requirements.txt
    and begin
        echo "[ setup ] Downloading Camoufox browser…"
        camoufox fetch
    end
    and touch $marker
    or begin
        echo "[ error ] Dependency installation failed."
        exit 1
    end
end

# ── Launch application ────────────────────────────────────────────────────────
echo "[ run ]   Starting FA & Inkbunny Downloader…"
python gui.py
