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
    this.dateOfBirth,
    this.gender,
    this.preferredContactMethod,
    this.addressLine1,
    this.addressLine2,
    this.city,
    this.region,
    this.postalCode,
    this.countryCode,
    this.locale,
    this.timezone,
    this.userType = 'customer',
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

  /// ISO date string (yyyy-MM-dd) as returned by the API; null if unset.
  final String? dateOfBirth;
  final String? gender;
  final String? preferredContactMethod;
  final String? addressLine1;
  final String? addressLine2;
  final String? city;
  final String? region;
  final String? postalCode;
  final String? countryCode;
  final String? locale;
  final String? timezone;

  /// Principal kind from the API ("customer" | "reseller"); drives portal
  /// routing after login.
  final String userType;
  final List<String> roles;
  final List<String> scopes;

  bool get isReseller => userType == 'reseller';

  String get fullName {
    final dn = displayName?.trim();
    if (dn != null && dn.isNotEmpty) return dn;
    return '$firstName $lastName'.trim();
  }

  /// fullName cleaned up for greetings: title-cased, consecutive duplicate
  /// words collapsed ("Hyperia hyperia" -> "Hyperia").
  String get greetingName {
    final words = fullName.split(RegExp(r'\s+'));
    final out = <String>[];
    for (final w in words) {
      if (w.isEmpty) continue;
      if (out.isNotEmpty && out.last.toLowerCase() == w.toLowerCase()) continue;
      out.add(w[0].toUpperCase() + w.substring(1));
    }
    return out.isEmpty ? fullName : out.join(' ');
  }

  String get initials {
    final f = firstName.isNotEmpty ? firstName[0] : '';
    final l = lastName.isNotEmpty ? lastName[0] : '';
    final result = (f + l).toUpperCase();
    return result.isEmpty ? '?' : result;
  }

  /// Round-trips with [Me.fromJson] for the on-device profile cache. Uses the
  /// same snake_case keys as the API so a cached blob and a fresh `/auth/me`
  /// response are interchangeable.
  Map<String, dynamic> toJson() => {
        'id': id,
        'first_name': firstName,
        'last_name': lastName,
        'email': email,
        'display_name': displayName,
        'avatar_url': avatarUrl,
        'email_verified': emailVerified,
        'phone': phone,
        'date_of_birth': dateOfBirth,
        'gender': gender,
        'preferred_contact_method': preferredContactMethod,
        'address_line1': addressLine1,
        'address_line2': addressLine2,
        'city': city,
        'region': region,
        'postal_code': postalCode,
        'country_code': countryCode,
        'locale': locale,
        'timezone': timezone,
        'roles': roles,
        'scopes': scopes,
      };

  factory Me.fromJson(Map<String, dynamic> json) => Me(
        id: json['id'].toString(),
        firstName: json['first_name'] as String? ?? '',
        lastName: json['last_name'] as String? ?? '',
        email: json['email'] as String? ?? '',
        displayName: json['display_name'] as String?,
        avatarUrl: json['avatar_url'] as String?,
        emailVerified: json['email_verified'] as bool? ?? false,
        phone: json['phone'] as String?,
        dateOfBirth: json['date_of_birth'] as String?,
        gender: json['gender'] as String?,
        preferredContactMethod: json['preferred_contact_method'] as String?,
        addressLine1: json['address_line1'] as String?,
        addressLine2: json['address_line2'] as String?,
        city: json['city'] as String?,
        region: json['region'] as String?,
        postalCode: json['postal_code'] as String?,
        countryCode: json['country_code'] as String?,
        locale: json['locale'] as String?,
        timezone: json['timezone'] as String?,
        userType: json['user_type'] as String? ?? 'customer',
        roles: (json['roles'] as List? ?? const [])
            .map((e) => e.toString())
            .toList(),
        scopes: (json['scopes'] as List? ?? const [])
            .map((e) => e.toString())
            .toList(),
      );
}
