# Ansible Collection - poettig.roles

Documentation for the collection.

### Usage

Just add the collection to your galaxy-requirements file using git.
The collection is currently not published to the ansible galaxy.

The version corresponds to the tag your want to use.
Use `main` if you always want to use the latest version.

```yml
collections:
  - name: poettig.roles
    source: https://github.com/poettig/ansible_roles.git
    version: 1.0.0
    type: git
```

### Development

Symlink this collection to the `collections/ansible_collections` of the ansible repo you are using for development.

```
mkdir -p collections/ansible_collections/poettig; ln -s <path to the ansible_roles checkout> collections/ansible_collections/poettig/roles
```
