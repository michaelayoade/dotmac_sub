import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../models/contact.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Manage the subscriber's additional contacts (GET/POST/PATCH/DELETE
/// /me/contacts). Parity with the web "Additional contacts" screen.
class ContactsScreen extends ConsumerWidget {
  const ContactsScreen({super.key});

  Future<void> _delete(
    BuildContext context,
    WidgetRef ref,
    Contact contact,
  ) async {
    final messenger = ScaffoldMessenger.of(context);
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Remove contact'),
        content: Text('Remove ${contact.displayName}?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Remove'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await ref.read(contactRepositoryProvider).delete(contact.id);
      ref.invalidate(contactsProvider);
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  void _openForm(BuildContext context, {Contact? existing}) {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _ContactFormSheet(existing: existing),
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final contacts = ref.watch(contactsProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Additional contacts')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _openForm(context),
        icon: const Icon(Icons.person_add_alt_1),
        label: const Text('Add'),
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(contactsProvider);
          await ref.read(contactsProvider.future);
        },
        child: AsyncValueView(
          value: contacts,
          onRetry: () => ref.invalidate(contactsProvider),
          data: (list) {
            if (list.isEmpty) {
              return ListView(
                children: const [
                  SizedBox(height: 100),
                  EmptyState(
                    icon: Icons.contacts_outlined,
                    message:
                        'No additional contacts yet.\nAdd people who can be reached about your account.',
                  ),
                ],
              );
            }
            return ListView(
              padding: const EdgeInsets.all(12),
              children: [
                for (final c in list) ...[
                  Card(
                    margin: EdgeInsets.zero,
                    child: ListTile(
                      leading: const Icon(Icons.person_outline),
                      title: Text(c.displayName),
                      subtitle: Text(
                        [
                          ...c.channels,
                          [
                            c.contactType,
                            if (c.relationship != null &&
                                c.relationship!.trim().isNotEmpty)
                              c.relationship!.trim(),
                          ].join(' · '),
                        ].join('\n'),
                      ),
                      isThreeLine: c.channels.isNotEmpty,
                      onTap: () => _openForm(context, existing: c),
                      trailing: PopupMenuButton<String>(
                        onSelected: (v) => v == 'edit'
                            ? _openForm(context, existing: c)
                            : _delete(context, ref, c),
                        itemBuilder: (_) => const [
                          PopupMenuItem(value: 'edit', child: Text('Edit')),
                          PopupMenuItem(value: 'remove', child: Text('Remove')),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                ],
                const SizedBox(height: 72), // clear the FAB
              ],
            );
          },
        ),
      ),
    );
  }
}

/// Add / edit a contact. A reasonable subset of fields plus the toggles;
/// requires at least one contact channel (mirrors the server's 400).
class _ContactFormSheet extends ConsumerStatefulWidget {
  const _ContactFormSheet({this.existing});
  final Contact? existing;

  @override
  ConsumerState<_ContactFormSheet> createState() => _ContactFormSheetState();
}

class _ContactFormSheetState extends ConsumerState<_ContactFormSheet> {
  late final _fullName = TextEditingController(
    text: widget.existing?.fullName ?? '',
  );
  late final _phone = TextEditingController(text: widget.existing?.phone ?? '');
  late final _email = TextEditingController(text: widget.existing?.email ?? '');
  late final _whatsapp = TextEditingController(
    text: widget.existing?.whatsapp ?? '',
  );
  late final _relationship = TextEditingController(
    text: widget.existing?.relationship ?? '',
  );

  late String _contactType = widget.existing?.contactType ?? 'general';
  late bool _isBillingContact = widget.existing?.isBillingContact ?? false;
  late bool _isAuthorized = widget.existing?.isAuthorized ?? false;
  late bool _receivesNotifications =
      widget.existing?.receivesNotifications ?? false;

  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _fullName.dispose();
    _phone.dispose();
    _email.dispose();
    _whatsapp.dispose();
    _relationship.dispose();
    super.dispose();
  }

  bool get _hasChannel =>
      _phone.text.trim().isNotEmpty ||
      _email.text.trim().isNotEmpty ||
      _whatsapp.text.trim().isNotEmpty;

  Future<void> _save() async {
    if (!_hasChannel) {
      setState(
        () => _error =
            'Add at least one way to reach them (phone, email, or WhatsApp).',
      );
      return;
    }
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    setState(() {
      _busy = true;
      _error = null;
    });

    String? trimOrNull(TextEditingController c) {
      final v = c.text.trim();
      return v.isEmpty ? null : v;
    }

    final body = <String, dynamic>{
      'full_name': trimOrNull(_fullName),
      'phone': trimOrNull(_phone),
      'email': trimOrNull(_email),
      'whatsapp': trimOrNull(_whatsapp),
      'relationship': trimOrNull(_relationship),
      'contact_type': _contactType,
      'is_billing_contact': _isBillingContact,
      'is_authorized': _isAuthorized,
      'receives_notifications': _receivesNotifications,
    };

    try {
      final repo = ref.read(contactRepositoryProvider);
      final result = widget.existing == null
          ? await repo.create(body)
          : await repo.update(widget.existing!.id, body);
      ref.invalidate(contactsProvider);
      navigator.pop();
      if (result.warnings.isNotEmpty) {
        messenger.showSnackBar(
          SnackBar(content: Text(result.warnings.join('\n'))),
        );
      } else {
        messenger.showSnackBar(
          SnackBar(
            content: Text(
              widget.existing == null ? 'Contact added' : 'Contact updated',
            ),
          ),
        );
      }
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final insets = MediaQuery.of(context).viewInsets.bottom;
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 16, 16, insets + 16),
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              widget.existing == null ? 'Add contact' : 'Edit contact',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 16),
            if (_error != null) ...[
              Text(
                _error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
              const SizedBox(height: 8),
            ],
            TextField(
              controller: _fullName,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(labelText: 'Full name'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _phone,
              keyboardType: TextInputType.phone,
              onChanged: (_) => setState(() {}),
              decoration: const InputDecoration(labelText: 'Phone'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _email,
              keyboardType: TextInputType.emailAddress,
              onChanged: (_) => setState(() {}),
              decoration: const InputDecoration(labelText: 'Email'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _whatsapp,
              keyboardType: TextInputType.phone,
              onChanged: (_) => setState(() {}),
              decoration: const InputDecoration(labelText: 'WhatsApp'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _relationship,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(
                labelText: 'Relationship (e.g. spouse, manager)',
              ),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: _contactType,
              decoration: const InputDecoration(labelText: 'Contact type'),
              items: [
                for (final t in contactTypes)
                  DropdownMenuItem(
                    value: t,
                    child: Text(t[0].toUpperCase() + t.substring(1)),
                  ),
              ],
              onChanged: (v) => setState(() => _contactType = v ?? 'general'),
            ),
            const SizedBox(height: 8),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Billing contact'),
              value: _isBillingContact,
              onChanged: (v) => setState(() => _isBillingContact = v),
            ),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Authorized'),
              subtitle: const Text('May act on the account'),
              value: _isAuthorized,
              onChanged: (v) => setState(() => _isAuthorized = v),
            ),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Receives notifications'),
              value: _receivesNotifications,
              onChanged: (v) => setState(() => _receivesNotifications = v),
            ),
            const SizedBox(height: 16),
            FilledButton(
              onPressed: _busy ? null : _save,
              child: _busy
                  ? const SizedBox(
                      height: 20,
                      width: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Text('Save'),
            ),
          ],
        ),
      ),
    );
  }
}
