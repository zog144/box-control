"""Advisory launch inspection. Never accepts, prepares, repairs or builds work."""
from contextlib import nullcontext
from pathlib import Path
import time

from .dependency import resolve
from .errors import ConfigurationError, DependencyError
from .inspection import RecoveryInspection, _snapshot, inspect_recovery
from .runtime.reference import RuntimeReferenceStore
from .runtime.systemd import _MINIMUM_SYSTEMD_VERSION
from .specification import discover_applications


def preflight_launch(control, application_name):
    if not isinstance(application_name, str) or not application_name:
        raise ValueError('application_name must be a nonempty string')
    result = dict(schema=1, application=application_name, observed_at=time.time(),
                  advisory=True, status='unable-to-verify', snapshot='unlocked',
                  checks=[], generation=None, replacement_runtime_ids=[],
                  replacement_basis='persisted-live-or-unresolved',
                  capability_scope='transport-connectivity-and-systemd-version')

    def check(code, status, message):
        result['checks'].append(dict(code=code, status=status, message=message))

    snapshot = RecoveryInspection()
    with _snapshot(control.project, snapshot) as available:
        result['snapshot'] = snapshot.snapshot
        if not available:
            check('project-snapshot', 'unable-to-verify', 'Retry inspection when project state is readable and idle.')
            return result
        try:
            control.project.require_state_allowed()
        except ConfigurationError as exc:
            check('project-location', 'blocked', str(exc))
        from .application_storage import ApplicationStorage
        try:
            ApplicationStorage(control.project).admission()
        except Exception as exc:
            check('preparation-admission', 'blocked', str(exc))
        recovery = inspect_recovery(control.project)
        if recovery.mutation_status != 'no-recorded-block':
            check('recovery', 'blocked' if recovery.mutation_status == 'recovery-required' else 'unable-to-verify',
                  'Inspect recovery_status or recovery_explanation before attempting launch.')
        else:
            check('recovery', 'ready', 'No recorded mutation block.')
        try:
            applications = discover_applications(control.project.application_dir)
            resolve(applications)
            if application_name not in applications:
                raise ConfigurationError(f'unknown application: {application_name}')
            application = applications[application_name]
            if any(not program.command for program in application.programs):
                raise ConfigurationError('Each program needs a nonempty command.')
            check('application-definition', 'ready', 'Application declarations and dependency ordering are valid.')
            store = RuntimeReferenceStore(control.project.runtime_reference_file)
            if not application.multiple_instances:
                result['replacement_runtime_ids'] = [r.runtime_id for r in store.launch_candidates(store.load(), application=application_name)]
            check('instance-policy', 'ready', 'Launch will re-observe singleton replacement candidates.' if not application.multiple_instances
                  else 'Launch creates an additional instance.')
        except (ConfigurationError, DependencyError) as exc:
            check('application-definition', 'blocked', str(exc))
        except Exception:
            check('application-state', 'unable-to-verify', 'Application definition or runtime records could not be inspected.')
        preview = getattr(control.image_provider, 'preview', None)
        if not callable(preview):
            check('image-inputs', 'unable-to-verify', 'Image provider does not support read-only preview.')
        else:
            try:
                selection = preview(control.project)
                if selection is None:
                    check('image-inputs', 'blocked', 'No published image is selected; publish and activate an image with image-build.')
                elif not Path(selection.root).is_dir() or selection.manifest.get('fingerprint') != selection.generation:
                    check('image-inputs', 'blocked', 'Selected generation is missing or its manifest identity disagrees.')
                else:
                    result['generation'] = selection.generation
                    check('image-inputs', 'ready', 'Provider validated the selected published generation.')
                    if application_name in applications:
                        from .mounts import validate
                        try:
                            for program in applications[application_name].programs:
                                validate(selection.root, program.mounts, program.command, require_targets=True)
                            check('mount-policy', 'ready', 'Data destinations preserve the immutable software tree.')
                        except Exception as exc:
                            check('mount-policy', 'blocked', str(exc))

                    if application_name in applications:
                        from .application_storage import ApplicationStorage
                        try:
                            bound = ApplicationStorage(control.project).bind(
                                applications[application_name], selection.generation
                            )
                            check('application-preparation', 'ready', 'Writable environment is compatible with the selected generation.')
                        except Exception as exc:
                            bound = None
                            check('application-preparation', 'blocked', str(exc))
                        if bound is not None and bound.software_view:
                            from .application_software import resolve_binding
                            try:
                                software = resolve_binding(control.project, bound, selection)
                                check(
                                    'application-software',
                                    'ready',
                                    'Verified the exact immutable application software view and referenced artifacts.',
                                )
                                result['software_view'] = software.software_view
                            except Exception as exc:
                                check('application-software', 'blocked', str(exc))
            except Exception:
                check('image-inputs', 'unable-to-verify', 'Image provider could not verify the current inputs.')
    # Never hold the project snapshot lock during a remote call. This check is
    # connectivity/version only, not proof that future privileged launch is authorized.
    budget = getattr(control.systemd_transport, 'operation_budget', None)
    try:
        with budget(5) if budget else nullcontext():
            version = control.systemd_transport.version()
        if version < _MINIMUM_SYSTEMD_VERSION:
            check('systemd-version', 'blocked', f'systemd {_MINIMUM_SYSTEMD_VERSION}+ is required; found {version}.')
        else:
            check('systemd-version', 'ready', f'Transport reachable; systemd {version}.')
    except Exception:
        check('systemd-version', 'unable-to-verify', 'Cannot verify systemd through the configured transport.')
    statuses = {c['status'] for c in result['checks']}
    result['status'] = ('blocked' if 'blocked' in statuses else
                        'unable-to-verify' if 'unable-to-verify' in statuses else 'ready-to-attempt')
    return result
