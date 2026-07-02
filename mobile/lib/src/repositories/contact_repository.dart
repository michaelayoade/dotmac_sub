import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/contact.dart';

/// A create/update result: the saved contact plus any non-blocking warnings.
class ContactSaveResult {
  ContactSaveResult({required this.contact, required this.warnings});

  final Contact contact;
  final List<String> warnings;

  factory ContactSaveResult.fromJson(Map<String, dynamic> json) =>
      ContactSaveResult(
        contact: Contact.fromJson(
          (json['contact'] as Map).cast<String, dynamic>(),
        ),
        warnings: (json['warnings'] as List? ?? const [])
            .map((e) => e.toString())
            .toList(),
      );
}

/// Wraps the self-scoped additional-contacts endpoints
/// (/me/contacts, app/api/me.py).
class ContactRepository {
  ContactRepository(this.dio);

  final Dio dio;

  /// GET /me/contacts — a bare JSON array of the subscriber's contacts.
  Future<List<Contact>> list() async {
    final data = await guard(() => dio.get('/me/contacts'));
    return (data as List)
        .cast<Map<String, dynamic>>()
        .map(Contact.fromJson)
        .toList();
  }

  /// POST /me/contacts — create a contact. The server requires at least one
  /// contact channel and returns 400 otherwise.
  Future<ContactSaveResult> create(Map<String, dynamic> body) async {
    final data = await guard(() => dio.post('/me/contacts', data: body));
    return ContactSaveResult.fromJson((data as Map).cast<String, dynamic>());
  }

  /// PATCH /me/contacts/{id} — update a contact (404 if not yours).
  Future<ContactSaveResult> update(String id, Map<String, dynamic> body) async {
    final data = await guard(() => dio.patch('/me/contacts/$id', data: body));
    return ContactSaveResult.fromJson((data as Map).cast<String, dynamic>());
  }

  /// DELETE /me/contacts/{id} — remove a contact.
  Future<void> delete(String id) async {
    await guard(() => dio.delete('/me/contacts/$id'));
  }
}
