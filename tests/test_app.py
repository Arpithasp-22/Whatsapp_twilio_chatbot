import importlib


def get_app(db, monkeypatch):
    """Import app lazily: it calls setup_bot() on import, which needs the DB to exist."""
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    import app as app_module
    return importlib.reload(app_module).app


def test_health(db, monkeypatch):
    assert get_app(db, monkeypatch).test_client().get("/health").json == {"status": "ok"}


def test_webhook_returns_twiml(db, monkeypatch):
    r = get_app(db, monkeypatch).test_client().post("/whatsapp", data={"Body": "hi", "From": "whatsapp:+911"})
    assert r.status_code == 200 and r.mimetype == "application/xml"
    assert b"<Message>" in r.data and b"Welcome" in r.data


def test_signature_enforced_when_token_set(db, monkeypatch):
    app = get_app(db, monkeypatch)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    r = app.test_client().post("/whatsapp", data={"Body": "hi", "From": "x"})
    assert r.status_code == 403
