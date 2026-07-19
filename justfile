# space-finder task runner

# default path to work with; override on the command line: just scan path=~/other
path := "~/dev"

# list available recipes
default:
    @just --list

# walk a path once and cache the result
scan target=path:
    ./space_finder.py scan {{target}}

# breakdown from cache
show target=path:
    ./space_finder.py show {{target}}

# largest directories anywhere under a path
top target=path:
    ./space_finder.py top {{target}}

# largest individual files under a path
files target=path:
    ./space_finder.py files {{target}}

# interactive drill-down browser
tui target=path:
    ./space_finder.py tui {{target}}

# rescan forever to keep the cache warm
watch target=path interval="300":
    ./space_finder.py watch {{target}} --interval {{interval}}

# list cached roots
roots:
    ./space_finder.py roots

# symlink onto PATH for a shorter command
link:
    mkdir -p ~/.local/bin
    ln -sf "$PWD/space_finder.py" ~/.local/bin/space-finder
    @echo "linked ~/.local/bin/space-finder"
