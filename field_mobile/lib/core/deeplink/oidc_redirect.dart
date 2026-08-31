/// The field app's VERIFIED redirect boundary.
///
/// The OIDC ceremony hands its authorization response back to the app over a
/// single permanent HTTPS origin that the operating system has cryptographically
/// bound to this application: an Android App Link (`assetlinks.json` +
/// `android:autoVerify`) and an iOS Universal Link (`apple-app-site-association`
/// + the Associated Domains entitlement).
///
/// Two shapes are deliberately absent and must stay absent:
///
///   * a CUSTOM SCHEME (`io.dotmac.field://...`). Any app on the device may
///     register the same scheme, so the OS hands the authorization code to
///     whoever asks first. There is no verification step at all.
///   * a WILDCARD redirect (`https://*.dotmac.io/...`, `pathPrefix`,
///     `pathPattern`). A wildcard turns every host or path that happens to match
///     into a legal place to deliver a code.
///
/// The origin is fleet-owned and deliberately NOT Sub's deployment hostname and
/// NOT a tenant hostname: the association documents pin ONE application
/// identity, so the origin has to outlive any single deployment or tenant.
library;

/// Documented default for [oidcCallbackOrigin].
const String kOidcCallbackOriginDefault = 'https://links.dotmac.io';

/// Documented default for [oidcCallbackPath].
const String kOidcCallbackPathDefault = '/oidc/field/callback';

/// Scheme of the redirect boundary. Not a knob: a verified redirect boundary is
/// HTTPS by construction, and [OidcRedirectConfig.parse] refuses anything else.
const String kOidcCallbackScheme = 'https';

/// Origin of the verified callback, overridable per build with
/// `--dart-define=OIDC_CALLBACK_ORIGIN=...` (or the `OIDC_CALLBACK_ORIGIN` key
/// in `field_mobile/brand.json`, which every build passes with
/// `--dart-define-from-file`).
///
/// Overriding it is a FIVE-file change — this default, `brand.json`, the Android
/// intent filter, the iOS Associated Domains entitlement, and both association
/// documents under `deploy/links.dotmac.io/` — because the OS validates the
/// native declarations against the documents, not against this string.
/// `scripts/check_field_applinks.py` fails CI when those five disagree.
const String oidcCallbackOrigin = String.fromEnvironment(
  'OIDC_CALLBACK_ORIGIN',
  defaultValue: kOidcCallbackOriginDefault,
);

/// Path of the verified callback, overridable with
/// `--dart-define=OIDC_CALLBACK_PATH=...`. See [oidcCallbackOrigin] for the
/// other four files that have to move with it.
const String oidcCallbackPath = String.fromEnvironment(
  'OIDC_CALLBACK_PATH',
  defaultValue: kOidcCallbackPathDefault,
);

/// The exact origin + path the OIDC ceremony is allowed to come back through.
///
/// Every comparison here is EXACT. There is no prefix match, no pattern, no
/// "close enough" host suffix, and no case-insensitive path: an incoming link
/// either is the one redirect boundary or it is not ours at all.
class OidcRedirectConfig {
  const OidcRedirectConfig._({
    required this.host,
    required this.port,
    required this.path,
  });

  /// Parses a configured origin/path pair, refusing anything that would widen
  /// the boundary. Throws [ArgumentError] rather than degrading: a build with a
  /// malformed redirect boundary must not start a ceremony at all.
  factory OidcRedirectConfig.parse({
    required String origin,
    required String path,
  }) {
    final parsed = Uri.tryParse(origin.trim());
    if (parsed == null || !parsed.hasScheme || !parsed.hasAuthority) {
      throw ArgumentError.value(
        origin,
        'origin',
        'is not an absolute URL (expected e.g. $kOidcCallbackOriginDefault)',
      );
    }
    if (parsed.scheme.toLowerCase() != kOidcCallbackScheme) {
      throw ArgumentError.value(
        origin,
        'origin',
        'must use https. A custom scheme is not a verified redirect boundary: '
            'any app on the device can claim it.',
      );
    }
    if (parsed.host.isEmpty || parsed.host.contains('*')) {
      throw ArgumentError.value(
        origin,
        'origin',
        'must name one exact host, with no wildcard',
      );
    }
    if (parsed.userInfo.isNotEmpty ||
        parsed.path.isNotEmpty ||
        parsed.hasQuery ||
        parsed.hasFragment) {
      throw ArgumentError.value(
        origin,
        'origin',
        'must be a bare scheme://host[:port] with no path, query, fragment or '
            'userinfo',
      );
    }

    final trimmedPath = path.trim();
    if (!trimmedPath.startsWith('/') || trimmedPath.length < 2) {
      throw ArgumentError.value(path, 'path', 'must be an absolute path');
    }
    if (trimmedPath.contains('*') ||
        trimmedPath.contains('?') ||
        trimmedPath.contains('#')) {
      throw ArgumentError.value(
        path,
        'path',
        'must be one exact path, with no wildcard, query or fragment',
      );
    }
    if (trimmedPath.endsWith('/')) {
      throw ArgumentError.value(
        path,
        'path',
        'must not end in a slash: the OS matches the declared path byte for '
            'byte, so a trailing slash silently declares a different path',
      );
    }

    return OidcRedirectConfig._(
      host: parsed.host.toLowerCase(),
      port: parsed.port,
      path: trimmedPath,
    );
  }

  /// The boundary this build was compiled with. Lazily built, so a misconfigured
  /// `--dart-define` throws where it is first used rather than at class load.
  static final OidcRedirectConfig fromBuild = OidcRedirectConfig.parse(
    origin: oidcCallbackOrigin,
    path: oidcCallbackPath,
  );

  /// Exact host the OS verified this app against.
  final String host;

  /// Port of the boundary (443 for a normal HTTPS origin).
  final int port;

  /// Exact callback path. Never a prefix.
  final String path;

  /// The `redirect_uri` to send to the identity provider. It must be registered
  /// with the provider verbatim.
  Uri get redirectUri =>
      Uri(scheme: kOidcCallbackScheme, host: host, port: port, path: path);

  /// True only for the one exact absolute URL this app accepts a callback on.
  bool matches(Uri incoming) {
    if (incoming.scheme.toLowerCase() != kOidcCallbackScheme) return false;
    if (!incoming.hasAuthority) return false;
    if (incoming.userInfo.isNotEmpty) return false;
    if (incoming.host.toLowerCase() != host) return false;
    if (incoming.port != port) return false;
    return incoming.path == path;
  }

  /// Re-anchors a platform ROUTE (path + query, which is all Flutter's deep-link
  /// handler delivers to the router — the host is consumed by the OS-level
  /// intent filter / associated domain) onto this boundary.
  ///
  /// Returns null unless the route is a bare absolute path that is exactly
  /// [path]. A route carrying its own scheme or authority is refused outright:
  /// that is the shape (`//evil.example/oidc/field/callback`) that would
  /// otherwise let a crafted route move the host.
  Uri? anchor(Uri location) {
    if (location.hasScheme || location.hasAuthority) return null;
    if (location.hasFragment) return null;
    if (!location.path.startsWith('/')) return null;
    if (location.path != path) return null;
    return Uri(
      scheme: kOidcCallbackScheme,
      host: host,
      port: port,
      path: location.path,
      query: location.hasQuery ? location.query : null,
    );
  }

  @override
  String toString() => 'OidcRedirectConfig($redirectUri)';
}
