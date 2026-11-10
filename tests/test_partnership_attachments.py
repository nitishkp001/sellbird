"""PDFs remain real MIME/Graph attachments, with no external delivery."""
import base64
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sellbird.config import EmailChannelConfig, EnvConfig
from sellbird.integrations.gmail import GmailProvider
from sellbird.integrations.microsoft import MicrosoftProvider
from sellbird.integrations.smtp_mail import SmtpImapProvider

ATTACHMENT={'filename':'brief.pdf','content_type':'application/pdf','data':b'%PDF-1.7 test-only'}


def config(provider):
    return SimpleNamespace(persona=SimpleNamespace(name='Sender',email='sender@example.com'),channels=SimpleNamespace(email=EmailChannelConfig(provider=provider)))


@pytest.mark.asyncio
async def test_smtp_carries_reviewed_pdf_with_plain_text_body(monkeypatch):
    send=AsyncMock();monkeypatch.setattr('sellbird.integrations.smtp_mail.aiosmtplib.send',send)
    provider=SmtpImapProvider(config('smtp'),EnvConfig(smtp_host='smtp.example.com',smtp_username='sender',smtp_password='test-only'))
    result=await provider.send_email('recipient@example.net','A brief','Body',attachments=(ATTACHMENT,))
    assert result.ok
    message=send.call_args.args[0]
    attachments=list(message.iter_attachments())
    assert len(attachments)==1 and attachments[0].get_content_type()=='application/pdf'
    assert attachments[0].get_payload(decode=True)==ATTACHMENT['data']
    assert message.get_body(preferencelist=('plain',)).get_content().strip()=='Body'


@pytest.mark.asyncio
async def test_gmail_carries_pdf_inside_raw_mime(monkeypatch):
    provider=GmailProvider(config('gmail'),EnvConfig())
    calls=[]
    async def request(method,url,**kwargs):
        calls.append((method,url,kwargs))
        return {'id':'sent-test','threadId':'thread-test'} if method=='POST' else {}
    monkeypatch.setattr(provider,'_request',request)
    result=await provider.send_email('recipient@example.net','A brief','Body',attachments=(ATTACHMENT,))
    assert result.ok
    message=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(calls[0][2]['json']['raw']))
    assert list(message.iter_attachments())[0].get_payload(decode=True)==ATTACHMENT['data']


@pytest.mark.asyncio
async def test_microsoft_adds_pdf_before_sending_draft(monkeypatch):
    provider=MicrosoftProvider(config('microsoft'),EnvConfig())
    monkeypatch.setattr(provider,'_check_identity',AsyncMock(return_value='sender@example.com'))
    calls=[]
    async def request(method,url,**kwargs):
        calls.append((method,url,kwargs))
        return {'id':'draft-test','internetMessageId':'<test@example.com>','conversationId':'thread-test'} if url=='/me/messages' else {}
    monkeypatch.setattr(provider,'_request',request)
    result=await provider.send_email('recipient@example.net','A brief','Body',attachments=(ATTACHMENT,))
    assert result.ok
    attachment=[c for c in calls if c[1].endswith('/attachments')][0]
    assert attachment[2]['json']['isInline'] is False
    assert base64.b64decode(attachment[2]['json']['contentBytes'])==ATTACHMENT['data']
    assert calls.index(attachment)<next(i for i,c in enumerate(calls) if c[1].endswith('/send'))
