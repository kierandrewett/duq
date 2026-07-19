# duq task runner

# default path to work with; override on the command line: just scan path=~/other
path := "~/dev"

# list available recipes
default:
    @just --list

# walk a path once and cache the result
scan target=path:
    ./duq scan {{target}}

# breakdown from cache
show target=path:
    ./duq show {{target}}

# largest directories anywhere under a path
top target=path:
    ./duq top {{target}}

# largest individual files under a path
files target=path:
    ./duq files {{target}}

# interactive drill-down browser
tui target=path:
    ./duq tui {{target}}

# rescan forever to keep the cache warm
watch target=path interval="300":
    ./duq watch {{target}} --interval {{interval}}

# list cached roots
roots:
    ./duq roots

# symlink onto PATH so `duq` works from any dir
link:
    mkdir -p ~/.local/bin
    ln -sf "$PWD/duq" ~/.local/bin/duq
    @echo "linked ~/.local/bin/duq -> $PWD/duq"

# install + enable the background daemon that keeps all cached roots warm
service interval="300":
    ./duq install-service --interval {{interval}}
    systemctl --user daemon-reload
    systemctl --user enable --now duq.service
    @echo "duq.service enabled"
