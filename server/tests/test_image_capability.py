from types import SimpleNamespace
import pytest
from app.harness_attachments import require_image_capability
from app.security import AppError

@pytest.mark.parametrize('entry', [{}, {'capabilities':['text']}, {'capabilities':['multimodal'],'enabled':False}])
def test_image_requires_explicit_capability(entry):
    service=SimpleNamespace(catalog=SimpleNamespace(models={'selected':entry}))
    with pytest.raises(AppError) as exc: require_image_capability(service,'selected')
    assert exc.value.code=='VISION_UNAVAILABLE'

def test_image_capability_is_rechecked_after_admin_revocation():
    entry={'capabilities':['multimodal']}
    service=SimpleNamespace(catalog=SimpleNamespace(models={'selected':entry}))
    require_image_capability(service,'selected')
    entry['capabilities']=[]
    with pytest.raises(AppError): require_image_capability(service,'selected')

def test_historical_image_cannot_cross_dispatch_after_revocation(monkeypatch):
    import threading
    from app.harness_attachments import AttachmentPrompt
    service=SimpleNamespace(catalog=SimpleNamespace(models={'selected':{'capabilities':[]}}),db=SimpleNamespace(lock=threading.RLock()))
    prompt=AttachmentPrompt.__new__(AttachmentPrompt)
    prompt.service=service;prompt.runtime=SimpleNamespace(conversation_id='owned')
    prompt.run={'model_id':'selected'};prompt.history=[{'id':'old-image','binding':{}}];prompt.snapshots=[]
    prompt.authorize=lambda: {'id':'owner'}
    monkeypatch.setattr('app.harness_attachments.preflight',lambda *_: [{'name':'old.png'}])
    admitted=[]
    with pytest.raises(AppError) as exc:prompt.dispatch(SimpleNamespace(session_prompt=lambda *args:admitted.append(args)),'native','read')
    assert exc.value.code=='VISION_UNAVAILABLE' and admitted==[]
