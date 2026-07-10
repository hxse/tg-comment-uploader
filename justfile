set shell := ["bash", "-eu", "-o", "pipefail", "-c"]
set positional-arguments

config := "config/config.json"
profile := "default"
retries := "5"

default:
    just --list

dev-shell:
    nix develop

check:
    uvx ruff format
    uvx ty check

test:
    uv run pytest

server:
    uv run python -m tg_comment_uploader server --config "{{config}}"

server-config config_path:
    uv run python -m tg_comment_uploader server --config "$1"

upload +paths:
    uv run python -m tg_comment_uploader upload --config "{{config}}" --profile "{{profile}}" --retries "{{retries}}" "$@"

upload-profile profile +paths:
    uv run python -m tg_comment_uploader upload --config "{{config}}" --profile "$1" --retries "{{retries}}" "${@:2}"

upload-retry retries +paths:
    uv run python -m tg_comment_uploader upload --config "{{config}}" --profile "{{profile}}" --retries "$1" "${@:2}"

upload-profile-retry profile retries +paths:
    uv run python -m tg_comment_uploader upload --config "{{config}}" --profile "$1" --retries "$2" "${@:3}"

upload-config config_path +paths:
    uv run python -m tg_comment_uploader upload --config "$1" --profile "{{profile}}" --retries "{{retries}}" "${@:2}"

upload-config-profile config_path profile +paths:
    uv run python -m tg_comment_uploader upload --config "$1" --profile "$2" --retries "{{retries}}" "${@:3}"

upload-config-retry config_path retries +paths:
    uv run python -m tg_comment_uploader upload --config "$1" --profile "{{profile}}" --retries "$2" "${@:3}"

upload-config-profile-retry config_path profile retries +paths:
    uv run python -m tg_comment_uploader upload --config "$1" --profile "$2" --retries "$3" "${@:4}"
