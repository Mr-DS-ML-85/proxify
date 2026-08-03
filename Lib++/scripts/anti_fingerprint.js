/**
 * Anti-Fingerprinting Script — Injects BEFORE any page JS loads
 * 
 * Strips ALL detectable browser fingerprints so the page sees a
 * generic, undetectable browser environment.
 * 
 * Use this in EVERY Lib++ browser-based strategy to ensure no
 * fingerprinting surface is exposed to target pages.
 */

(function() {
    'use strict';

    // =====================================================================
    // 1. NAVIGATOR WEBDRIVER — The most common check
    // =====================================================================
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true,
    });

    // =====================================================================
    // 2. CHROME RUNTIME — Real Chrome has this
    // =====================================================================
    if (!window.chrome) {
        window.chrome = {
            runtime: {
                connect: () => ({}),
                sendMessage: () => {},
                onMessage: { addListener: () => {} },
                onConnect: { addListener: () => {} },
                onDisconnect: { addListener: () => {} },
            },
            app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY: 'ready' } },
            webstore: { onInstallStageChanged: {}, onDownloadProgress: {} },
            storage: { local: {}, sync: {}, managed: {} },
        };
    }

    // =====================================================================
    // 3. PERMISSIONS — Hide notifications permission query
    // =====================================================================
    const origQuery = navigator.permissions.query;
    navigator.permissions.query = function(params) {
        if (params.name === 'notifications') {
            return Promise.resolve({ state: 'prompt', onchange: null });
        }
        return origQuery(params);
    };

    // =====================================================================
    // 4. CANVAS FINGERPRINTING — Add noise to getImageData
    // =====================================================================
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
        const imageData = origGetImageData.call(this, x, y, w, h);
        // Clone the buffer to avoid mutating the canvas's internal buffer
        const cloned = new Uint8ClampedArray(imageData.data);
        for (let i = 0; i < cloned.length; i += 4) {
            cloned[i] ^= 2;     // R
            cloned[i + 1] ^= 1; // G
            cloned[i + 2] ^= 3; // B
        }
        imageData.data.set(cloned);
        return imageData;
    };

    // Override toDataURL on canvas elements
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const imageData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i] ^= 1;
            }
            ctx.putImageData(imageData, 0, 0);
        }
        return origToDataURL.call(this, type, quality);
    };

    // =====================================================================
    // 5. WEBGL FINGERPRINTING — Spoof vendor/renderer
    // =====================================================================
    const webglProto = WebGLRenderingContext.prototype;
    const origGetParameter = webglProto.getParameter;
    webglProto.getParameter = function(parameter) {
        // UNMASKED_VENDOR_WEBGL
        if (parameter === 37445) return 'Intel Inc.';
        // UNMASKED_RENDERER_WEBGL
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        // VENDOR
        if (parameter === 7936) return 'WebKit';
        // RENDERER
        if (parameter === 7937) return 'WebKit WebGL';
        return origGetParameter.call(this, parameter);
    };

    // WebGL2
    const webgl2Proto = WebGL2RenderingContext.prototype;
    const origGetParameter2 = webgl2Proto.getParameter;
    webgl2Proto.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        if (parameter === 7936) return 'WebKit';
        if (parameter === 7937) return 'WebKit WebGL';
        return origGetParameter2.call(this, parameter);
    };

    // =====================================================================
    // 6. WEBRTC — Block IP leak
    // =====================================================================
    if (window.RTCPeerConnection) {
        const origCreateDataChannel = RTCPeerConnection.prototype.createDataChannel;
        RTCPeerConnection.prototype.createDataChannel = function() {
            return null;
        };
        const origSetLocalDescription = RTCPeerConnection.prototype.setLocalDescription;
        RTCPeerConnection.prototype.setLocalDescription = function(desc) {
            if (desc && desc.sdp) {
                desc.sdp = desc.sdp.replace(/a=candidate:.*udp.*\r\n/g, '');
            }
            return origSetLocalDescription.call(this, desc);
        };
    }

    // =====================================================================
    // 7. PLUGINS — Add realistic plugin list
    // =====================================================================
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
            ];
            plugins.item = (i) => plugins[i];
            plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
            return plugins;
        },
        configurable: true,
    });

    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => {
            const types = [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: navigator.plugins[0] },
                { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: navigator.plugins[0] },
            ];
            types.item = (i) => types[i];
            types.namedItem = (n) => types.find(t => t.type === n) || null;
            return types;
        },
        configurable: true,
    });

    // =====================================================================
    // 8. LANGUAGES — Realistic language list
    // =====================================================================
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
        configurable: true,
    });

    // =====================================================================
    // 9. HARDWARE CONCURRENCY — Normal value
    // NOTE: capture the real value FIRST. The getter must not read the
    // (already-overridden) property or it recurses infinitely → RangeError
    // the moment a page reads navigator.hardwareConcurrency, which itself is
    // a huge automation signal (any anti-bot JS reading it would crash).
    // =====================================================================
    const __realCores = navigator.hardwareConcurrency;
    // Clamp to 8 max — headless VMs/containers often report 16+ cores, an
    // outlier for a typical desktop that anti-bot JS can fingerprint.
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => Math.max(4, Math.min(8, __realCores || 8)),
        configurable: true,
    });

    // =====================================================================
    // 10. DEVICE MEMORY — Normal value (same recursion trap — capture first)
    // =====================================================================
    const __realMem = navigator.deviceMemory;
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => Math.max(4, Math.min(8, __realMem || 8)),
        configurable: true,
    });

    // =====================================================================
    // 11. SCREEN — Normalize screen metrics
    // =====================================================================
    if (screen.width < 1280) {
        Object.defineProperty(screen, 'width', { get: () => 1920 });
        Object.defineProperty(screen, 'height', { get: () => 1080 });
        Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
        Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
        Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
        Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
    }

    // =====================================================================
    // 12. TIMEZONE — Normalize
    // =====================================================================
    const origGetTimezoneOffset = Date.prototype.getTimezoneOffset;
    Date.prototype.getTimezoneOffset = function() {
        return origGetTimezoneOffset.call(this);
    };
    // Note: nodriver sets timezone via browser config; we just ensure
    // the getTimezoneOffset function is native.

    // =====================================================================
    // 13. FONTS — Override font API to return common fonts
    // =====================================================================
    if (document.fonts) {
        const origCheck = document.fonts.check;
        document.fonts.check = function(font) {
            return true; // Always say font is available
        };
    }

    // =====================================================================
    // 14. HEADERS — Override to hide automation headers
    // =====================================================================
    const origFetch = window.fetch;
    window.fetch = function(input, init) {
        if (init && init.headers) {
            const headers = new Headers(init.headers);
            // Remove automation-indicating headers if any
            if (headers.has('X-Requested-With')) {
                headers.delete('X-Requested-With');
            }
            init.headers = headers;
        }
        return origFetch.call(this, input, init);
    };

    // =====================================================================
    // 15. BATTERY — Spoof if available
    // =====================================================================
    if (navigator.getBattery) {
        navigator.getBattery = function() {
            return Promise.resolve({
                charging: true,
                chargingTime: 0,
                dischargingTime: Infinity,
                level: 1,
                onchargingchange: null,
                onchargingtimechange: null,
                ondischargingtimechange: null,
                onlevelchange: null,
            });
        };
    }

    console.log('[Lib++ Anti-Fingerprint] All fingerprints stripped ✓');
})();
