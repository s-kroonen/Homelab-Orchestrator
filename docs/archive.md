# Archive (soft delete)

Deleting a service does not destroy it. It moves to the **Archive** page for
`ARCHIVE_RETENTION_DAYS` (30) days, can be restored in one click, and is purged after that.

## What is kept

The service and every sub-service that went with it, in the registry file's own shape:
settings, probes and proxy hosts. Entries are made when a service is deleted from the
dashboard, the API or the CLI, when a stack delete removes its services, and when a registry
reload (Reset to saved, an older `services.yaml` put back) drops services that were not in the file.

Not kept: backup records, runs and health state. They are not tied to the registry rows and
behave as after any delete. A database dump file on disk is also untouched.

## Restoring

Archive page → **Restore**, or `orchestrator-cli archive restore <id|slug>`, or the button on
the "was deleted" page an old alert link lands on. Restore is refused, and changes nothing, when:

* a service with that slug exists again (rename or delete it first);
* a sub-service's parent no longer exists;
* the service's node no longer exists.

Smaller problems are repaired and reported as notes: a backup policy that is gone is left off,
`depends_on` / `backup_requires` entries for services that no longer exist are dropped, and a
proxy host whose hostname another service now uses comes back switched off.
Restored proxy hosts are not yet pushed to Traefik Manager; Save pushes them. Save to YAML
afterwards to make the restore permanent in `services.yaml`.

## Purging

A job removes expired entries every six hours (and the Archive page and CLI purge on read).
**Delete now** removes one immediately.

## Pages that lead nowhere

A browser opening an address that does not exist gets a "not found" page instead of raw JSON.
For `/services/<slug>` it checks the archive: a recently deleted service is named, with when and by
whom, and a Restore button; otherwise it says there is no such service. Only a signed-in admin sees
what used to exist. API calls keep the JSON 404.

## CLI

```
orchestrator-cli archive list [--json]
orchestrator-cli archive restore <id|slug>
orchestrator-cli archive purge <id|slug> [--yes]
```
