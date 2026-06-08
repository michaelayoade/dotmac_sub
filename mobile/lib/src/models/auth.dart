// Auth-flow models mirroring app/schemas/auth_flow.py.

/// Response from POST /auth/login.
///
/// When MFA is enabled, `accessToken` is null, `mfaRequired` is true and
/// `mfaToken` must be exchanged at POST /auth/mfa/verify.
class LoginResult {
  LoginResult({
    this.accessToken,
    this.refreshToken,
    this.tokenType = 'bearer',
    this.mfaRequired = false,
    this.mfaToken,
  });

  final String? accessToken;
  final String? refreshToken;
  final String tokenType;
  final bool mfaRequired;
  final String? mfaToken;

  bool get isAuthenticated => accessToken != null && !mfaRequired;

  factory LoginResult.fromJson(Map<String, dynamic> json) => LoginResult(
        accessToken: json['access_token'] as String?,
        refreshToken: json['refresh_token'] as String?,
        tokenType: json['token_type'] as String? ?? 'bearer',
        mfaRequired: json['mfa_required'] as bool? ?? false,
        mfaToken: json['mfa_token'] as String?,
      );
}

/// Response from POST /auth/mfa/verify and POST /auth/refresh.
class TokenPair {
  TokenPair({required this.accessToken, this.refreshToken});

  final String accessToken;
  final String? refreshToken;

  factory TokenPair.fromJson(Map<String, dynamic> json) => TokenPair(
        accessToken: json['access_token'] as String,
        refreshToken: json['refresh_token'] as String?,
      );
}

/// Response from GET /auth/me (MeResponse).
class Me {
  Me({
    required this.id,
    required this.firstName,
    required this.lastName,
    required this.email,
    this.displayName,
    this.avatarUrl,
    this.emailVerified = false,
    this.phone,
    this.locale,
    this.timezone,
    this.roles = const [],
    this.scopes = const [],
  });

  final String id;
  final String firstName;
  final String lastName;
  final String email;
  final String? displayName;
  final String? avatarUrl;
  final bool emailVerified;
  final String? phone;
  final String? locale;
  final String? timezone;
  final List<String> roles;
  final List<String> scopes;

  String get fullName {
    final dn = displayName?.trim();
    if (dn != null && dn.isNotEmpty) return dn;
    return '$firstName $lastName'.trim();
  }

  String get initials {
    final f = firstName.isNotEmpty ? firstName[0] : '';
    final l = lastName.isNotEmpty ? lastName[0] : '';
    final result = (f + l).toUpperCase();
    return result.isEmpty ? '?' : result;
  }

  factory Me.fromJson(Map<String, dynamic> json) => Me(
        id: json['id'].toString(),
        firstName: json['first_name'] as String? ?? '',
        lastName: json['last_name'] as String? ?? '',
        email: json['email'] as String? ?? '',
        displayName: json['display_name'] as String?,
        avatarUrl: json['avatar_url'] as String?,
        emailVerified: json['email_verified'] as bool? ?? false,
        phone: json['phone'] as String?,
        locale: json['locale'] as String?,
        timezone: json['timezone'] as String?,
        roles: (json['roles'] as List? ?? const [])
            .map((e) => e.toString())
            .toList(),
        scopes: (json['scopes'] as List? ?? const [])
            .map((e) => e.toString())
            .toList(),
      );
}
