import 'dart:convert';

import 'package:crypto/crypto.dart';

/// The principal/tenant pair that owns a row or a file on this device.
///
/// Sub issues one deployment per operator, so the tenant dimension is the
/// deployment the technician is signed in to (plus an explicit tenant claim
/// when the token carries one), and the principal dimension is the
/// authenticated subject and its type. Both are required: two technicians on
/// one deployment, and one technician against two deployments, are different
/// scopes and must never share a byte of local storage.
class DataScope {
  const DataScope({required this.tenant, required this.principal});

  /// The scope every unauthenticated launch starts in. It holds nothing: the
  /// store refuses to bind it, so no row or file can be written before we know
  /// who the device belongs to.
  static const unbound = DataScope(tenant: '', principal: '');

  final String tenant;
  final String principal;

  bool get isBound => tenant.isNotEmpty && principal.isNotEmpty;

  /// Opaque, stable identifier written into every row and used as the storage
  /// directory name. Hashed so no directory listing or database dump leaks a
  /// deployment host or a subject id.
  String get key {
    final material = utf8.encode('$tenant $principal');
    return sha256.convert(material).toString();
  }

  @override
  bool operator ==(Object other) =>
      other is DataScope &&
      other.tenant == tenant &&
      other.principal == principal;

  @override
  int get hashCode => Object.hash(tenant, principal);

  @override
  String toString() => 'DataScope(${isBound ? key : "unbound"})';
}

/// Normalizes an API base URL down to the deployment identity. Scheme, host and
/// port only: a trailing slash or a path prefix must not fork a technician's
/// storage into two scopes.
String deploymentIdentity(String baseUrl) {
  final parsed = Uri.tryParse(baseUrl.trim());
  if (parsed == null || parsed.host.isEmpty) return baseUrl.trim();
  final port = parsed.hasPort ? ':${parsed.port}' : '';
  return '${parsed.scheme}://${parsed.host.toLowerCase()}$port';
}

/// Derives the storage scope from decoded access-token claims. Returns null
/// when the token carries no usable subject: callers must treat that as "stay
/// unbound" rather than inventing a scope, because a guessed scope is a scope
/// two principals could end up sharing.
DataScope? dataScopeFromClaims(
  Map<String, dynamic>? claims, {
  required String baseUrl,
}) {
  if (claims == null) return null;
  final subject = _claim(claims, 'principal_id') ?? _claim(claims, 'sub');
  if (subject == null) return null;
  final principalType = _claim(claims, 'principal_type') ?? 'unknown';
  final tenantClaim = _claim(claims, 'tenant_id') ?? _claim(claims, 'tid');
  final deployment = deploymentIdentity(baseUrl);
  if (deployment.isEmpty) return null;
  return DataScope(
    tenant: tenantClaim == null ? deployment : '$deployment#$tenantClaim',
    principal: '$principalType:$subject',
  );
}

String? _claim(Map<String, dynamic> claims, String name) {
  final value = claims[name];
  if (value == null) return null;
  final text = value.toString().trim();
  return text.isEmpty ? null : text;
}
