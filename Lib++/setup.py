"""
Lib++ — Next-Generation Anti-Bot Strategy Layer
Installed as `Lib_plus_plus` import name while keeping `Lib++` directory.
"""

from setuptools import setup, find_packages

setup(
    name="lib-plus-plus",
    version="1.3.0",
    description="Next-Generation Anti-Bot Strategy & TLS Fingerprinting Layer",
    long_description=open("README.md").read() if __import__("os").path.exists("README.md") else "",
    long_description_content_type="text/markdown",
    author="Proxify Team",
    python_requires=">=3.10",
    packages=[
        "Lib_plus_plus",
        "Lib_plus_plus.core",
        "Lib_plus_plus.strategies",
        "Lib_plus_plus.adapters",
        "Lib_plus_plus.engine",
        "Lib_plus_plus.processors",
        "Lib_plus_plus.scripts",
    ],
    package_dir={
        "Lib_plus_plus": ".",
        "Lib_plus_plus.core": "core",
        "Lib_plus_plus.strategies": "strategies",
        "Lib_plus_plus.adapters": "adapters",
        "Lib_plus_plus.engine": "engine",
        "Lib_plus_plus.processors": "processors",
        "Lib_plus_plus.scripts": "scripts",
    },
    include_package_data=True,
    install_requires=[
        "httpx>=0.27.0",
        "curl-cffi>=0.7.0",
        "beautifulsoup4>=4.12.0",
        "markdownify>=0.11.0",
        "brotli>=1.1.0",
    ],
    extras_require={
        "full": [
            "httpx-curl-cffi>=0.4.0",
            "aioquic>=1.0.0",
            "websockets>=12.0",
            "nodriver>=0.38.0",
            "cryptography>=41.0.0",
            "uvicorn>=0.27.0",
            "fastapi>=0.109.0",
            "prometheus-client>=0.19.0",
            "python-dotenv>=1.0.0",
        ],
    },
)
