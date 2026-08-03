"""
SSL MITM Certificate Generator
Dynamically creates a Root CA and signs domain-specific leaf certificates for HTTPS interception.
"""

import datetime
import os
import re
import ssl
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

CERTS_DIR = Path(".certs")
CA_KEY_PATH = CERTS_DIR / "proxy_orchestratorCA.key"
CA_CERT_PATH = CERTS_DIR / "proxy_orchestratorCA.pem"

_CA_KEY = None
_CA_CERT = None


def setup_ca():
    """Ensure the Root CA exists, create if missing."""
    global _CA_KEY, _CA_CERT
    CERTS_DIR.mkdir(exist_ok=True)

    if CA_KEY_PATH.exists() and CA_CERT_PATH.exists():
        with open(CA_KEY_PATH, "rb") as f:
            _CA_KEY = serialization.load_pem_private_key(f.read(), password=None)
        with open(CA_CERT_PATH, "rb") as f:
            _CA_CERT = x509.load_pem_x509_certificate(f.read())
        return

    print("Generating new Proxify Root CA...")
    # Generate CA key
    _CA_KEY = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    
    # Generate CA cert
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"Proxify"),
        x509.NameAttribute(NameOID.COMMON_NAME, u"Proxify Root CA"),
    ])
    _CA_CERT = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        _CA_KEY.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.utcnow() - datetime.timedelta(days=1)
    ).not_valid_after(
        datetime.datetime.utcnow() + datetime.timedelta(days=3650)
    ).add_extension(
        x509.BasicConstraints(ca=True, path_length=None), critical=True,
    ).sign(_CA_KEY, hashes.SHA256())

    with open(CA_KEY_PATH, "wb") as f:
        f.write(_CA_KEY.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
    with open(CA_CERT_PATH, "wb") as f:
        f.write(_CA_CERT.public_bytes(serialization.Encoding.PEM))
    
    print(f"Root CA generated: {CA_CERT_PATH}")
    print("WARNING: You must trust this Root CA in your OS or browser to avoid SSL errors.")


_VALID_DOMAIN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def generate_leaf_cert(domain: str) -> tuple[str, str]:
    """Generates a leaf cert signed by the Root CA for the given domain."""
    if not _CA_KEY:
        setup_ca()

    # domain comes from the client's CONNECT target; reject anything that could
    # escape CERTS_DIR or otherwise not be a hostname.
    if not _VALID_DOMAIN.match(domain) or ".." in domain:
        raise ValueError(f"invalid domain for certificate: {domain!r}")

    cert_path = CERTS_DIR / f"{domain}.crt"
    key_path = CERTS_DIR / f"{domain}.key"

    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    # Generate leaf key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Generate leaf cert
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, domain),
    ])
    
    builder = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        _CA_CERT.subject
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.utcnow() - datetime.timedelta(days=1)
    ).not_valid_after(
        datetime.datetime.utcnow() + datetime.timedelta(days=397)
    ).add_extension(
        x509.SubjectAlternativeName([x509.DNSName(domain)]),
        critical=False,
    )

    certificate = builder.sign(_CA_KEY, hashes.SHA256())

    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))

    with open(cert_path, "wb") as f:
        f.write(certificate.public_bytes(serialization.Encoding.PEM))

    return str(cert_path), str(key_path)


def get_ssl_context(domain: str) -> ssl.SSLContext:
    """Returns an SSLContext configured with a cert for the given domain."""
    cert_file, key_file = generate_leaf_cert(domain)
    
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    context.set_alpn_protocols(["http/1.1"])
    return context
