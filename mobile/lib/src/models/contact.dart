/// An additional contact on the subscriber's account
/// (GET/POST/PATCH/DELETE /me/contacts).
class Contact {
  Contact({
    required this.id,
    required this.subscriberId,
    this.fullName,
    this.phone,
    this.email,
    this.whatsapp,
    this.facebook,
    this.instagram,
    this.xHandle,
    this.telegram,
    this.linkedin,
    this.otherSocial,
    this.relationship,
    this.contactType = 'general',
    this.isBillingContact = false,
    this.isAuthorized = false,
    this.receivesNotifications = false,
    this.notes,
    this.createdAt,
    this.updatedAt,
  });

  final String id;
  final String subscriberId;
  final String? fullName;
  final String? phone;
  final String? email;
  final String? whatsapp;
  final String? facebook;
  final String? instagram;
  final String? xHandle;
  final String? telegram;
  final String? linkedin;
  final String? otherSocial;
  final String? relationship;
  final String contactType;
  final bool isBillingContact;
  final bool isAuthorized;
  final bool receivesNotifications;
  final String? notes;
  final DateTime? createdAt;
  final DateTime? updatedAt;

  /// Display name, falling back to the first available channel.
  String get displayName {
    final n = fullName?.trim();
    if (n != null && n.isNotEmpty) return n;
    return phone ?? email ?? whatsapp ?? 'Contact';
  }

  /// The contact channels that are actually filled in, for a compact subtitle.
  List<String> get channels => [
        if (_has(phone)) phone!.trim(),
        if (_has(email)) email!.trim(),
        if (_has(whatsapp)) 'WhatsApp ${whatsapp!.trim()}',
      ];

  static bool _has(String? v) => v != null && v.trim().isNotEmpty;

  factory Contact.fromJson(Map<String, dynamic> json) => Contact(
        id: json['id'].toString(),
        subscriberId: json['subscriber_id'].toString(),
        fullName: json['full_name'] as String?,
        phone: json['phone'] as String?,
        email: json['email'] as String?,
        whatsapp: json['whatsapp'] as String?,
        facebook: json['facebook'] as String?,
        instagram: json['instagram'] as String?,
        xHandle: json['x_handle'] as String?,
        telegram: json['telegram'] as String?,
        linkedin: json['linkedin'] as String?,
        otherSocial: json['other_social'] as String?,
        relationship: json['relationship'] as String?,
        contactType: json['contact_type'] as String? ?? 'general',
        isBillingContact: json['is_billing_contact'] as bool? ?? false,
        isAuthorized: json['is_authorized'] as bool? ?? false,
        receivesNotifications: json['receives_notifications'] as bool? ?? false,
        notes: json['notes'] as String?,
        createdAt: _date(json['created_at']),
        updatedAt: _date(json['updated_at']),
      );

  static DateTime? _date(dynamic v) =>
      v == null ? null : DateTime.tryParse(v.toString());
}

/// The channel fields where at least one must be present for a valid contact.
const contactChannelFields = <String>[
  'phone',
  'email',
  'whatsapp',
  'facebook',
  'instagram',
  'x_handle',
  'telegram',
  'linkedin',
  'other_social',
];

/// The selectable contact types (server default: general).
const contactTypes = <String>[
  'general',
  'billing',
  'technical',
  'installation',
  'emergency',
];
