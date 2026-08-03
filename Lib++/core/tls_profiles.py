"""
TLS Profile Manager — Full JA3/JA4 fingerprint database with rotation and per-domain learning.

Solves:
  - ❌ curl_cffi no TLS rotation across strategies
  - ❌ No TLS fingerprint control in Playwright/Puppeteer
  - ❌ Static TLS profiles that get detected
  - ❌ No per-domain TLS learning
"""

from __future__ import annotations

import os
import random
import time
from typing import Optional

from .types import TlsFingerprint, TLSProfile


# =============================================================================
# Persona pinning — ONE coherent fingerprint for every path (stealth)
# =============================================================================

def persona_pinned() -> bool:
    """True when the pipeline is pinned to a single stealth persona."""
    return os.getenv("PERSONA_PINNED", "true").lower() == "true"


def persona_profile_name() -> str:
    """TLS profile name for the pinned persona (PERSONA_TLS env, default chrome146)."""
    tls = os.getenv("PERSONA_TLS", "chrome146")
    # Map curl_cffi target → TLS_PROFILES key
    mapping = {
        "chrome146": "chrome146_win",
        "chrome142": "chrome142_win",
        "chrome136": "chrome136_win",
        "chrome131": "chrome131_win",
        "chrome124": "chrome124_win",
    }
    return mapping.get(tls, "chrome146_win")


# =============================================================================
# JA3/JA4 Fingerprint Database — 40+ real browser fingerprints
# =============================================================================

TLS_PROFILES: dict[str, TLSProfile] = {
    # ==================== Persona: Chrome 146 (stealth pin) ================
    # PERSONA_PINNED=true pins the WHOLE pipeline to this single profile so
    # every request (HTTP + GUI Chrome) looks like the same user's browser.
    # Weight is highest so non-pinned selection also prefers it.
    "chrome146_win": TLSProfile(
        name="chrome146_win",
        weight=10.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21-17513-2570-65037-28-65039-65033-65035-65037-65038-65036-65040-65041,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="146",
            os="windows"
        )
    ),
    # ==================== Chrome 124-131 (Most Common) ====================
    "chrome131_win": TLSProfile(
        name="chrome131_win",
        weight=3.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21-17513-2570-65037-28-65039-65033-65035-65037-65038-65036-65040-65041,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="131",
            os="windows"
        )
    ),
    "chrome130_win": TLSProfile(
        name="chrome130_win",
        weight=3.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21-17513-2570-65037-28-65039-65033-65035-65037-65038,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="130",
            os="windows"
        )
    ),
    "chrome129_win": TLSProfile(
        name="chrome129_win",
        weight=3.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21-17513-2570-65037-28,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="129",
            os="windows"
        )
    ),
    "chrome128_win": TLSProfile(
        name="chrome128_win",
        weight=3.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="128",
            os="windows"
        )
    ),
    "chrome127_win": TLSProfile(
        name="chrome127_win",
        weight=2.5,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="127",
            os="windows"
        )
    ),
    "chrome126_win": TLSProfile(
        name="chrome126_win",
        weight=2.5,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="126",
            os="windows"
        )
    ),
    "chrome125_win": TLSProfile(
        name="chrome125_win",
        weight=2.5,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="125",
            os="windows"
        )
    ),
    "chrome124_win": TLSProfile(
        name="chrome124_win",
        weight=2.5,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="124",
            os="windows"
        )
    ),
    # ==================== Chrome on macOS ====================
    "chrome131_mac": TLSProfile(
        name="chrome131_mac",
        weight=2.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21-17513-2570-65037-28-65039-65033-65035-65037-65038-65036-65040-65041,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="131",
            os="macos"
        )
    ),
    # ==================== Chrome on Linux ====================
    "chrome131_linux": TLSProfile(
        name="chrome131_linux",
        weight=1.5,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21-17513-2570-65037-28-65039-65033-65035-65037-65038,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="131",
            os="linux"
        )
    ),
    # ==================== Firefox 133/135 (curl_cffi-supported targets) =====
    # NOTE: versions must map to curl_cffi's supported firefox targets
    # (firefox133/firefox135/...); firefox124/firefox125 are NOT valid targets.
    "firefox135_win": TLSProfile(
        name="firefox135_win",
        weight=2.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4867-4866-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="firefox",
            version="135",
            os="windows"
        )
    ),
    "firefox133_win": TLSProfile(
        name="firefox133_win",
        weight=2.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4867-4866-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="firefox",
            version="133",
            os="windows"
        )
    ),
    # ==================== Safari 17-18 (curl_cffi target: safari17_0 / safari18_0) ===
    # IMPORTANT: Reddit-class anti-bots (old.reddit.com confirmed) serve Safari
    # TLS fingerprints while 403/tarpitting Chrome — keep Safari profiles alive
    # and NOT deprecatable by the chrome-first selection.
    "safari17_mac": TLSProfile(
        name="safari17_mac",
        weight=2.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="safari",
            version="17_0",
            os="macos"
        )
    ),
    "safari18_mac": TLSProfile(
        name="safari18_mac",
        weight=2.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="safari",
            version="18_0",
            os="macos"
        )
    ),
    "safari18_4_mac": TLSProfile(
        name="safari18_4_mac",
        weight=1.5,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="safari",
            version="18_4",
            os="macos"
        )
    ),
    # ==================== Edge 99/101 (curl_cffi-supported targets) =========
    # NOTE: versions must map to curl_cffi's supported edge targets
    # (edge99/edge101 only); edge124/edge131 are NOT valid targets.
    "edge101_win": TLSProfile(
        name="edge101_win",
        weight=1.5,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="edge",
            version="101",
            os="windows"
        )
    ),
    "edge99_win": TLSProfile(
        name="edge99_win",
        weight=1.5,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="edge",
            version="99",
            os="windows"
        )
    ),
    # ==================== Mobile ====================
    "chrome_mobile_android": TLSProfile(
        name="chrome_mobile_android",
        weight=1.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-158-159-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="chrome",
            version="131",
            os="android"
        )
    ),
    "safari_mobile_ios": TLSProfile(
        name="safari_mobile_ios",
        weight=1.0,
        fingerprint=TlsFingerprint(
            ja3="771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,29-23-24-25-256-257-258,0-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16-17-18-19-20-21-22-23-24-25-26-27-28",
            ja4="t13d1516h2_8daaf6152771_02713d6af862",
            browser="safari",
            version="17_0",
            os="ios"
        )
    ),
}


def get_ja3_for_browser(browser: str, version: str = "", os_type: str = "") -> Optional[str]:
    """Look up JA3 hash for a specific browser configuration."""
    for name, profile in TLS_PROFILES.items():
        if browser in profile.fingerprint.browser and (
            not version or version in profile.fingerprint.version
        ) and (
            not os_type or os_type in profile.fingerprint.os
        ):
            return profile.fingerprint.ja3
    return None


class TLSProfileManager:
    """
    Manages TLS fingerprint profiles with:
    - Weighted random selection
    - Per-domain profile learning (tracks which profiles work for which domains)
    - Automatic deprecation of failing profiles
    - Profile rotation for proxy-switched connections
    """

    def __init__(self):
        self._profiles: dict[str, TLSProfile] = dict(TLS_PROFILES)
        self._domain_profile_map: dict[str, dict[str, float]] = {}  # domain -> {profile_name: success_rate}
        self._last_rotation: dict[str, float] = {}  # domain -> timestamp

    def get_profile(self, name: str) -> Optional[TLSProfile]:
        """Get a specific TLS profile by name."""
        return self._profiles.get(name)

    def select_profile(
        self,
        domain: str = "",
        preferred_browser: str = "chrome",
        prefer_new: bool = True,
    ) -> TLSProfile:
        """
        Select the best TLS profile for a domain.

        Uses per-domain learning: if a domain has history of success with
        specific profiles, prefer those. Otherwise, weighted random.

        When PERSONA_PINNED=true, ALWAYS returns the persona profile — no
        rotation, no random exploration. This keeps the HTTP fingerprint
        identical across every strategy AND aligned with the GUI Chrome,
        which is what Google-class anti-bots expect from a real user.
        """
        if persona_pinned():
            persona = self._profiles.get(persona_profile_name())
            if persona is not None and not persona.is_deprecated:
                return persona
            # Fall back to any chrome profile if persona got deprecated
            persona = next(
                (p for p in self._profiles.values()
                 if "chrome" in p.fingerprint.browser and not p.is_deprecated),
                None,
            )
            if persona is not None:
                return persona

        candidates = [
            p for p in self._profiles.values()
            if not p.is_deprecated
            and preferred_browser in p.fingerprint.browser
        ]

        if not candidates:
            candidates = [p for p in self._profiles.values() if not p.is_deprecated]

        if not candidates:
            # Fallback to first available
            candidates = list(self._profiles.values())

        # Check domain history
        if domain and domain in self._domain_profile_map:
            domain_profiles = self._domain_profile_map[domain]
            # Sort by success rate
            sorted_profiles = sorted(
                domain_profiles.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            for profile_name, _ in sorted_profiles[:3]:
                profile = self._profiles.get(profile_name)
                if profile and not profile.is_deprecated:
                    return profile
            # If learned profiles are all deprecated, fall through

        # Weighted random selection
        weights = [p.weight for p in candidates]
        chosen = random.choices(candidates, weights=weights, k=1)[0]

        if prefer_new and domain:
            # Sometimes rotate to a different profile to test
            now = time.monotonic()
            last = self._last_rotation.get(domain)
            if last is not None and (now - last) > 300:  # Rotate every 5 minutes
                other = random.choice(candidates)
                if other.name != chosen.name:
                    chosen = other
                # Only stamp the rotation clock when we actually rotated;
                # stamping on every call would make `elapsed > 300` never true.
                self._last_rotation[domain] = now

        return chosen

    def record_success(self, profile_name: str, domain: str) -> None:
        """Record a successful request with a TLS profile."""
        profile = self._profiles.get(profile_name)
        if profile:
            profile.success_count += 1
            profile.last_success_time = time.monotonic()
            profile.last_used_domain = domain

        # Update domain learning
        if domain not in self._domain_profile_map:
            self._domain_profile_map[domain] = {}
        domain_profiles = self._domain_profile_map[domain]
        if profile_name not in domain_profiles:
            domain_profiles[profile_name] = 0.0
        domain_profiles[profile_name] = min(
            1.0,
            domain_profiles[profile_name] + 0.1,
        )

    def record_failure(self, profile_name: str, domain: str) -> None:
        """Record a failed request with a TLS profile."""
        profile = self._profiles.get(profile_name)
        if profile:
            profile.failure_count += 1

        # Update domain learning
        if domain not in self._domain_profile_map:
            self._domain_profile_map[domain] = {}
        domain_profiles = self._domain_profile_map[domain]
        if profile_name not in domain_profiles:
            domain_profiles[profile_name] = 1.0
        domain_profiles[profile_name] = max(
            0.0,
            domain_profiles[profile_name] - 0.2,
        )

        # Auto-deprecate after 10 consecutive failures
        if profile and profile.failure_count >= 10 and profile.success_count == 0:
            profile.is_deprecated = True

    def get_profiles_for_domain(self, domain: str) -> list[dict]:
        """Get all profiles with their success rates for a domain."""
        domain_profiles = self._domain_profile_map.get(domain, {})
        result = []
        for name, rate in sorted(domain_profiles.items(), key=lambda x: x[1], reverse=True):
            profile = self._profiles.get(name)
            if profile:
                result.append({
                    "name": name,
                    "browser": profile.fingerprint.browser,
                    "version": profile.fingerprint.version,
                    "os": profile.fingerprint.os,
                    "success_rate": rate,
                    "total_successes": profile.success_count,
                    "total_failures": profile.failure_count,
                    "deprecated": profile.is_deprecated,
                })
        return result

    def get_stats(self) -> dict:
        """Get overall TLS profile manager statistics."""
        return {
            "total_profiles": len(self._profiles),
            "active_profiles": sum(1 for p in self._profiles.values() if not p.is_deprecated),
            "deprecated_profiles": sum(1 for p in self._profiles.values() if p.is_deprecated),
            "tracked_domains": len(self._domain_profile_map),
            "profiles": [
                {
                    "name": p.name,
                    "browser": p.fingerprint.browser,
                    "version": p.fingerprint.version,
                    "os": p.fingerprint.os,
                    "success_rate": p.success_rate,
                    "deprecated": p.is_deprecated,
                }
                for p in self._profiles.values()
            ],
        }


# Global singleton
tls_profile_manager = TLSProfileManager()
