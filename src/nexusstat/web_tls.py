"""TLS pro lokální web (M6-4B): kontext serveru z existujících souborů PEM.

Certifikát ani klíč se nikdy negenerují a nekopírují; jejich umístění,
důvěru klientů a obnovu řeší správce (docs/OPERATIONS.md). Samostatný modul,
aby web.py zůstal bez přístupu k souborovému systému.
"""

import os
import ssl
import stat


def tls_context(cert_path, key_path):
    """TLS serveru z existujících souborů PEM (certifikát a klíč); nic nevytváří. ValueError s důvodem.

    Klíč musí být obyčejný soubor vlastníka bez přístupu skupiny a ostatních
    (ne symlink) a bez hesla: šifrovaný klíč se odmítne, OpenSSL se na nic neptá.
    """
    try:
        info = os.lstat(key_path)
    except OSError as error:
        raise ValueError(f"klíč TLS {key_path} nelze přečíst ({error.strerror or error})") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"klíč TLS {key_path} musí být obyčejný soubor, ne symlink")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError(f"klíč TLS {key_path} smí patřit jen tomuto uživateli a číst jen on (chmod 600)")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert_path, key_path, password=b"")
    except (ssl.SSLError, OSError) as error:
        reason = getattr(error, "reason", None) or getattr(error, "strerror", None) or type(error).__name__
        raise ValueError(f"certifikát nebo klíč TLS nelze načíst ({reason}); šifrovaný klíč není podporován") \
            from None
    return context
