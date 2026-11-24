from __future__ import annotations

"""Diagnose outbound HTTPS/DNS/proxy connectivity from the machine running worker.py.

Run from the backend virtual environment:
    python network_diagnostic.py

This script does not authenticate, change configuration, write files, or scrape data.
It reports DNS, direct TCP/TLS, and HTTPX (environment-proxy-aware) outcomes separately.
Proxy values are never printed because they may contain credentials.
"""

import os
import socket
import ssl
import time
from urllib.parse import urlparse

import httpx

URLS = {
    "NSE web": "https://www.nseindia.com/",
    "NSE apex": "https://nseindia.com/",
    "NSE archive primary": "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "NSE archive alternate": "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "NiftyTrader": "https://www.niftytrader.in/results-calendar",
    "Flash Finance": "https://flashfinance.news/results-calendar/",
    "Business Standard": "https://www.business-standard.com/companies/results/upcoming-results-list",
}


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _dns(host: str) -> list[tuple[int, str, tuple]]:
    start = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        print(f"  DNS_FAIL elapsed_ms={_ms(start)} error={type(exc).__name__}:{exc}")
        return []
    results: list[tuple[int, str, tuple]] = []
    seen: set[tuple[int, str]] = set()
    for family, socktype, proto, _canonname, sockaddr in infos:
        ip = str(sockaddr[0])
        marker = (family, ip)
        if marker in seen:
            continue
        seen.add(marker)
        results.append((family, ip, sockaddr))
    print(f"  DNS_OK elapsed_ms={_ms(start)} addresses={','.join(('IPv6' if f == socket.AF_INET6 else 'IPv4') + ':' + ip for f, ip, _ in results)}")
    return results


def _direct_tls(host: str, address: tuple[int, str, tuple]) -> None:
    family, ip, sockaddr = address
    label = "IPv6" if family == socket.AF_INET6 else "IPv4"
    start = time.perf_counter()
    raw = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    raw.settimeout(5.0)
    try:
        raw.connect(sockaddr)
        tcp_ms = _ms(start)
        context = ssl.create_default_context()
        with context.wrap_socket(raw, server_hostname=host) as secured:
            tls_ms = _ms(start)
            print(f"  DIRECT_TLS_OK address={label}:{ip} tcp_ms={tcp_ms} tls_total_ms={tls_ms} tls={secured.version()}")
    except Exception as exc:
        print(f"  DIRECT_TLS_FAIL address={label}:{ip} elapsed_ms={_ms(start)} error={type(exc).__name__}:{str(exc)[:180]}")
        try:
            raw.close()
        except OSError:
            pass


def _httpx_check(label: str, url: str, trust_env: bool) -> None:
    start = time.perf_counter()
    try:
        response = httpx.get(
            url,
            timeout=httpx.Timeout(12.0, connect=8.0),
            follow_redirects=True,
            trust_env=trust_env,
            headers={"User-Agent": "Mozilla/5.0 (compatible; InvestmentIntelligenceDiagnostics/1.0)"},
        )
        print(f"  HTTPX_{'ENV' if trust_env else 'DIRECT'}_OK elapsed_ms={_ms(start)} status={response.status_code} final_host={urlparse(str(response.url)).hostname} bytes={len(response.content)}")
    except Exception as exc:
        print(f"  HTTPX_{'ENV' if trust_env else 'DIRECT'}_FAIL elapsed_ms={_ms(start)} error={type(exc).__name__}:{str(exc)[:220]}")


def main() -> None:
    print("Investment Intelligence outbound HTTPS diagnostics")
    proxy_keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy")
    print("Proxy environment variables present:", {key: bool(os.getenv(key)) for key in proxy_keys})
    for label, url in URLS.items():
        host = urlparse(url).hostname
        if not host:
            continue
        print(f"\n[{label}] {url}")
        addresses = _dns(host)
        # Check only one address of each family to keep this diagnostic bounded.
        families_seen: set[int] = set()
        for address in addresses:
            if address[0] in families_seen:
                continue
            families_seen.add(address[0])
            _direct_tls(host, address)
        _httpx_check(label, url, trust_env=True)
    print("\nInterpretation: DNS failures indicate resolver issues; direct TCP/TLS failures indicate route/firewall/ISP/remote-edge reachability; direct TLS success with HTTPX-ENV failure points toward proxy/environment settings; HTTP 403/429 means reachable but rejected/throttled, not ConnectTimeout.")


if __name__ == "__main__":
    main()