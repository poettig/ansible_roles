# Backup role: `borg-compact` SSH passphrase forwarding

When `backup_install_borg_compact` is enabled, remote compactions send the repository passphrase using the SSH environment variable `BORG_PASSPHRASE`. The SSH server running the remote Borg command must permit that variable.

Set `backup_borg_compact_acceptenv_hosts` to a list of dictionaries. Each dictionary specifies the inventory `host` that executes remote compactions and the SSH daemon's service name. The role also automatically adds hosts in the `backup_targets` inventory group. Ensure that variable is defined for every host in the group. The role deploys an sshd drop-in to each host with `delegate_to` after installing `/usr/local/bin/borg-compact`, and reloads that host's specified service only when the drop-in changes. The default is an empty list, so no manually configured hosts are added.

For example:

```yaml
backup_install_borg_compact: true
backup_borg_compact_acceptenv_hosts:
  - host: backup-runner
    sshd_service_name: ssh
```

Each `host` must be reachable and configured in inventory, and `sshd_service_name` must match the SSH service name on that host (commonly `sshd`, or `ssh` on Debian systems).

Only enable this for trusted SSH clients and hosts: the passphrase is intentionally available in the remote Borg process environment. The drop-in continues to accept `LANG` and `LC_*` as well as `BORG_PASSPHRASE`.