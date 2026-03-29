#!/usr/bin/env bash

cleanup() {
	# Delete password file
	rm "$pwfile" || echo "FAILED TO DELETE $pwfile, DO SO IMMEDIATELY MANUALLY!"

	# Reset trap
	trap - EXIT INT TERM
}

cleanup_and_exit() {
	cleanup
	exit
}

ssh_or_no_ssh() {
	local mode="$1"
	local borg_path="$2"
	local ssh_target="$3"
	shift 3

	if [ "$mode" = "ssh" ]; then
		# shellcheck disable=SC2154
		# (defined via tmux variable declaration)
		# -q for silence, -t for tty allocation - without it, SIGINT will not terminate the remote command
		time BORGPP="$password" ssh -qt -o SendEnv=BORGPP "$ssh_target" BORG_PASSPHRASE='$BORGPP' "$borg_path" "$@"
	elif [ "$mode" = "direct" ]; then
		time BORG_PASSPHRASE="$password" borg "$@"
	else
		echo "Invalid mode $mode"
		return 1
	fi
}

run_compact() {
	local compact_mode=$(echo "$1" | jq -r '.mode')
	local compact_name=$(echo "$1" | jq -r '.name')
	local compact_borg_path=$(echo "$1" | jq -r '.borg_path')
	local compact_ssh_target=$(echo "$1" | jq -r '.ssh_target')
	local compact_repo_path=$(echo "$1" | jq -r '.repo_path')

	if [ "$compact_ssh_target" = "null" ]; then
			local compact_location="$compact_repo_path"
	else
			local compact_location="$compact_ssh_target:$compact_repo_path"
	fi

	echo "Compacting $compact_name at $compact_location..."
	echo "======================================"
	echo

    echo "1. Get initial repo size"
    if ! ssh_or_no_ssh "$compact_mode" "$compact_borg_path" "$compact_ssh_target" info "$compact_repo_path"; then
		echo "ERROR: Failed to get repo information."
		echo
		echo
		return
	fi
    echo

	echo "2. Check integrity"
	if ! ssh_or_no_ssh "$compact_mode" "$compact_borg_path" "$compact_ssh_target" -vp check --verify-data "$compact_repo_path"; then
		echo "ERROR: Failed integrity check. Trying repair, CTRL+C to cancel..."
		echo
		if ! ssh_or_no_ssh "$compact_mode" "$compact_borg_path" "$compact_ssh_target" -vp check --repair --verify-data "$compact_repo_path"; then
			echo "CRITICAL: Failed repair."
			echo
			echo
			return
		fi
	fi
	echo

	echo "3. Compact"
	ssh_or_no_ssh "$compact_mode" "$compact_borg_path" "$compact_ssh_target" -vp compact "$compact_repo_path"
	echo

	echo
	echo
}

if [[ "$1" == "--source" ]]; then
	return
fi

SCRIPTPATH="$( cd -- "$(dirname "$0")" >/dev/null 2>&1 || exit ; pwd -P )"
configs=(
	'{"mode": "ssh", "name": "dalek", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de", "repo_path": "/mnt/vault101/backup/dalek", "getpass_cmd": "rbw get a81a9c4a-d0ad-42c7-8ac2-ac2787906271"}'
	'{"mode": "direct", "name": "dalek-BB", "repo_path": "ssh://sksy31tv@sksy31tv.repo.borgbase.com/./repo", "getpass_cmd": "rbw get a81a9c4a-d0ad-42c7-8ac2-ac2787906271"}'
	'{"mode": "ssh", "name": "PEET-PC-MANJARO", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de", "repo_path": "/mnt/vault101/backup/PEET-PC-MANJARO", "getpass_cmd": "rbw get 3ef24dd3-4f3b-4c80-974c-f6eae1881866"}'
	'{"mode": "direct", "name": "PEET-PC-MANJARO-BB", "repo_path": "ssh://gpbmwura@gpbmwura.repo.borgbase.com/./repo", "getpass_cmd": "rbw get 3ef24dd3-4f3b-4c80-974c-f6eae1881866"}'
	'{"mode": "ssh", "name": "Stinkewolke", "borg_path": "/mnt/backups/.local/bin/borg", "ssh_target": "backup@edi.oettig.de", "repo_path": "/mnt/backups/stinkewolke.oettig.de", "getpass_cmd": "rbw get 66739091-270d-414b-b1b7-67a9d0e44fe1"}'
	'{"mode": "direct", "name": "Stinkewolke-BB", "repo_path": "ssh://if3iq8gb@if3iq8gb.repo.borgbase.com/./repo", "getpass_cmd": "rbw get 66739091-270d-414b-b1b7-67a9d0e44fe1"}'
	'{"mode": "ssh", "name": "Citadel", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de","repo_path":"/mnt/vault101/backup/citadel.oettig.de", "getpass_cmd": "rbw get 0d33d27b-f2ab-4e40-bb20-1d6657f35e63"}'
	'{"mode": "direct", "name": "Citadel-BB", "repo_path":"ssh://uucz612r@uucz612r.repo.borgbase.com/./repo", "getpass_cmd": "rbw get 0d33d27b-f2ab-4e40-bb20-1d6657f35e63"}'
	'{"mode": "ssh", "name": "Afterlife", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de","repo_path":"/mnt/vault101/backup/afterlife.oettig.de", "getpass_cmd": "rbw get d8cdbb48-f4b2-4955-9e6b-ef4eba3efb7e"}'
	'{"mode": "direct", "name": "Afterlife-BB", "repo_path":"ssh://kdn82k7p@kdn82k7p.repo.borgbase.com/./repo", "getpass_cmd": "rbw get d8cdbb48-f4b2-4955-9e6b-ef4eba3efb7e"}'
	'{"mode": "ssh", "name": "EDI", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de","repo_path":"/mnt/vault101/backup/edi.oettig.de", "getpass_cmd": "rbw get b6052f69-2f8f-47bb-838f-7de279d1a407"}'
	'{"mode": "direct", "name": "EDI-BB", "repo_path":"ssh://uki17soh@uki17soh.repo.borgbase.com/./repo", "getpass_cmd": "rbw get b6052f69-2f8f-47bb-838f-7de279d1a407"}'
	'{"mode": "ssh", "name": "MassRelay", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de","repo_path":"/mnt/vault101/backup/massrelay.oettig.de", "getpass_cmd": "rbw get 46303e88-5261-43da-bb27-42466cf3f016"}'
	'{"mode": "direct", "name": "MassRelay-BB", "repo_path":"ssh://hp5313yx@hp5313yx.repo.borgbase.com/./repo", "getpass_cmd": "rbw get 46303e88-5261-43da-bb27-42466cf3f016"}'
	'{"mode": "ssh", "name": "Melonpan", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de","repo_path":"/mnt/vault101/backup/melonpan.lightmotif.tv", "getpass_cmd": "rbw get 766724f9-dc3f-4a09-9961-ea9480aa7ff5"}'
	'{"mode": "direct", "name": "Melonpan-BB", "repo_path":"ssh://cbdw9257@cbdw9257.repo.borgbase.com/./repo", "getpass_cmd": "rbw get 766724f9-dc3f-4a09-9961-ea9480aa7ff5"}'
	'{"mode": "ssh", "name": "Lemonsquash", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de","repo_path":"/mnt/vault101/backup/lemonsquash.lightmotif.tv", "getpass_cmd": "rbw get 96f14af8-5433-43e6-b157-34237361bae3"}'
	'{"mode": "direct", "name": "Lemonsquash-BB", "repo_path":"ssh://dp49c83z@dp49c83z.repo.borgbase.com/./repo", "getpass_cmd": "rbw get 96f14af8-5433-43e6-b157-34237361bae3"}'
	'{"mode": "ssh", "name": "Legacy", "borg_path": "/mnt/vault101/backup/.local/bin/borg", "ssh_target": "backup@stinkewolke.oettig.de","repo_path":"/mnt/vault101/backup/legacy.oettig.de", "getpass_cmd": "rbw get 7ed3d745-b3a5-45e7-be95-d0b71b954a34"}'
	'{"mode": "direct", "name": "Legacy-BB", "repo_path":"ssh://sbx60ttp@sbx60ttp.repo.borgbase.com/./repo", "getpass_cmd": "rbw get 7ed3d745-b3a5-45e7-be95-d0b71b954a34"}'
)

# Check if ssh agent available
if [ -z "$SSH_AUTH_SOCK" ]; then
	echo "SSH agent not available, please connect with agent forwarding."
	exit 1
fi

if tmux has-session -t compact 2> /dev/null; then
	echo "There already is a compaction session running, please close it before running again."
	exit 1
fi

# Ask for all passwords
declare -a pass_cmds
for config in "${configs[@]}"; do
	pass_cmds+=("$(echo "$config" | jq -r '.getpass_cmd')")
done

pwfile=$(mktemp)
trap cleanup_and_exit EXIT INT TERM
echo "Please put the result of the following list of commands into the file $pwfile."
printf "%s\n" "${pass_cmds[@]}"
# shellcheck disable=SC2162
read -p "Press enter to continue."

# Run compaction sequence
for i in "${!configs[@]}"; do
        compact_name=$(echo "${configs[$i]}" | jq -r '.name')

        # Skip if not the selected compaction
        if [ -n "$1" ] && ! [ "$compact_name" = "$1" ]; then
                continue
        fi

        if tmux has-session -t compact 2> /dev/null; then
			tmux new-window -t compact -n "$compact_name"
        else
			tmux new-session -d -s compact -n "$compact_name"
        fi

        tmux send-keys "unset HISTFILE" Enter
        tmux send-keys "password=\$(sed \"$((i+1))q;d\" $pwfile)" Enter
        tmux send-keys "source $SCRIPTPATH/$(basename "$0") --source" Enter
        tmux send-keys "run_compact '${configs[$i]}'" Enter
done

# Clean up password file after a short delay (tmux sometimes takes a bit to complete processing key inputs)
sleep 5
cleanup

if ! tmux has-session -t compact 2> /dev/null; then
	echo No compaction session started. Did you try to select a nonexistent compaction?
	exit 1
fi

tmux a -t compact
