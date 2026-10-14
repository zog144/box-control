import json
from dataclasses import replace
from pathlib import Path
import pytest
from zog.image_build.filesystem import inventory
from zog.image_build.metadata import identity
from zog.box_control.api import BoxControl
from zog.box_control.images import ImageBuildProvider, ImageProviderError
from zog.box_control.project import Project
from zog.box_control.model import ApplicationSpec, ProgramSpec
from zog.box_control.runtime.systemd import service_definition
from zog.root_control.systemd import BusctlSystemdBackend, SystemdBackendError


def publish(project, kind='image', activate=True):
    inputs={'schema':2,'kind':kind,'targets':['hello']}
    generation=identity(inputs)
    directory=project.state_dir/'image-build/generations'/generation
    root=directory/'root';root.mkdir(parents=True)
    (root/'hello').write_text('hello')
    manifest={'schema':2,'generation':generation,'kind':kind,'inputs':inputs,'outputs':inventory(root),'packages':{}}
    (directory/'manifest.json').write_text(json.dumps(manifest))
    if activate:
        (project.state_dir/'image-build/active').symlink_to(Path('generations')/generation)
    return generation,root


def test_default_provider_and_missing_image_are_read_only(tmp_path):
    project=Project(tmp_path/'absent');c=BoxControl(project)
    assert isinstance(c.image_provider,ImageBuildProvider)
    assert c.image_provider.preview(project) is None
    with pytest.raises(ImageProviderError,match='run image-build'):c.image_provider.ensure(project)
    assert not project.path.exists()


def test_published_image_is_selected_without_builder(tmp_path,monkeypatch):
    import zog.image_build as image_build
    monkeypatch.setattr(image_build,'ImageBuild',lambda **kw:pytest.fail('must not compile'))
    project=Project(tmp_path/'project');generation,root=publish(project)
    before={str(p):p.read_bytes() for p in project.path.rglob('*') if p.is_file()}
    selection=ImageBuildProvider().ensure(project)
    assert selection.generation==generation and selection.root==root
    assert selection.manifest['fingerprint']==generation and selection.manifest['schema']==2
    assert before=={str(p):p.read_bytes() for p in project.path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('damage',['outputs','manifest','foreign','missing','root-link'])
def test_invalid_published_selection_blocks(tmp_path,damage):
    project=Project(tmp_path/'project');generation,root=publish(project)
    if damage=='outputs':(root/'hello').write_text('changed')
    elif damage=='manifest':(root.parent/'manifest.json').write_text('{}')
    elif damage=='foreign':
        active=project.state_dir/'image-build/active';active.unlink();active.symlink_to(tmp_path)
    elif damage=='missing':(root.parent/'manifest.json').unlink()
    else:
        (root/'hello').unlink();root.rmdir();root.symlink_to(tmp_path)
    with pytest.raises(ImageProviderError):ImageBuildProvider().ensure(project)


@pytest.mark.parametrize('kind',['host-bootstrap','seed-check','native-check'])
def test_non_application_generations_are_rejected(tmp_path,kind):
    project=Project(tmp_path/'project');publish(project,kind)
    with pytest.raises(ImageProviderError,match='published image'):ImageBuildProvider().ensure(project)


def test_backend_accepts_exact_published_generation_not_only_active(tmp_path):
    project=Project(tmp_path/'demo');generation,root=publish(project,activate=False)
    project.mounts_dir.mkdir(parents=True, exist_ok=True)
    application=ApplicationSpec(name='hello',programs=(ProgramSpec(name='main',command=('/hello',)),))
    definition=service_definition(project,application,'ABC234',application.programs[0],generation_root=root)
    BusctlSystemdBackend._validate_service_definition(project.path,generation,definition.to_transport_dict())
    (root/'hello').write_text('changed')
    with pytest.raises(SystemdBackendError,match='outputs changed'):
        BusctlSystemdBackend._validate_service_definition(project.path,generation,definition.to_transport_dict())


def test_launch_and_preflight_use_published_provider(tmp_path):
    import importlib.util
    spec=importlib.util.spec_from_file_location('published_fixtures',Path(__file__).with_name('test_application_control.py'))
    f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
    project=Project(tmp_path/'project');f.write_application(project)
    c=f.control_for(project,tmp_path,f.FakeTransport(),['boot']);c.image_provider=ImageBuildProvider()
    generation,root=publish(project)
    assert c.preflight_application_launch('desktop')['generation']==generation
    runtime=c.launch_application('desktop')
    assert runtime.generation==generation
    assert root.is_dir()
