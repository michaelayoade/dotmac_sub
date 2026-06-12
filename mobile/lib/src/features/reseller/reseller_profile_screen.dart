import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/reseller.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Reseller organization profile: contact details (editable) and two-factor
/// authentication enrollment — parity with the web /reseller/profile page.
class ResellerProfileScreen extends ConsumerStatefulWidget {
  const ResellerProfileScreen({super.key});

  @override
  ConsumerState<ResellerProfileScreen> createState() =>
      _ResellerProfileScreenState();
}

class _ResellerProfileScreenState extends ConsumerState<ResellerProfileScreen> {
  final _email = TextEditingController();
  final _phone = TextEditingController();
  final _notes = TextEditingController();
  bool _seeded = false;
  bool _saving = false;

  @override
  void dispose() {
    _email.dispose();
    _phone.dispose();
    _notes.dispose();
    super.dispose();
  }

  void _seed(ResellerProfile p) {
    if (_seeded) return;
    _seeded = true;
    _email.text = p.contactEmail ?? '';
    _phone.text = p.contactPhone ?? '';
    _notes.text = p.notes ?? '';
  }

  Future<void> _save() async {
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _saving = true);
    try {
      await ref.read(resellerRepositoryProvider).updateProfile(
            contactEmail: _email.text,
            contactPhone: _phone.text,
            notes: _notes.text,
          );
      ref.invalidate(resellerProfileProvider);
      messenger.showSnackBar(const SnackBar(content: Text('Profile updated.')));
    } catch (_) {
      messenger.showSnackBar(
          const SnackBar(content: Text('Could not save profile.')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _enrollMfa() async {
    final repo = ref.read(resellerRepositoryProvider);
    final messenger = ScaffoldMessenger.of(context);
    final ResellerMfaSetup setup;
    try {
      setup = await repo.mfaSetup();
    } catch (_) {
      messenger.showSnackBar(
          const SnackBar(content: Text('Could not start 2FA setup.')));
      return;
    }
    if (!mounted) return;
    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _MfaSetupSheet(setup: setup),
    );
    if (ok == true) {
      ref.invalidate(resellerProfileProvider);
      messenger.showSnackBar(
          const SnackBar(content: Text('Two-factor authentication enabled.')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final profile = ref.watch(resellerProfileProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Profile & security')),
      body: AsyncValueView<ResellerProfile>(
        value: profile,
        onRetry: () => ref.invalidate(resellerProfileProvider),
        data: (p) {
          _seed(p);
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Text(p.name, style: Theme.of(context).textTheme.titleLarge),
              if (p.code != null)
                Text('Code: ${p.code}',
                    style: Theme.of(context).textTheme.bodySmall),
              const SizedBox(height: 16),
              TextField(
                controller: _email,
                keyboardType: TextInputType.emailAddress,
                decoration: const InputDecoration(
                  labelText: 'Contact email',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _phone,
                keyboardType: TextInputType.phone,
                decoration: const InputDecoration(
                  labelText: 'Contact phone',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _notes,
                maxLines: 3,
                decoration: const InputDecoration(
                  labelText: 'Notes',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              FilledButton(
                onPressed: _saving ? null : _save,
                child: Text(_saving ? 'Saving…' : 'Save profile'),
              ),
              const SizedBox(height: 24),
              Text('Security', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              Card(
                child: ListTile(
                  leading: Icon(
                    p.mfaEnabled ? Icons.verified_user : Icons.shield_outlined,
                    color: p.mfaEnabled
                        ? Theme.of(context).colorScheme.primary
                        : null,
                  ),
                  title: Text(p.mfaEnabled
                      ? 'Two-factor authentication is on'
                      : 'Two-factor authentication is off'),
                  subtitle: Text(p.mfaEnabled
                      ? '${p.mfaMethods.where((m) => m.verified).length} verified method(s)'
                      : 'Protect your reseller account with an authenticator app'),
                  trailing: p.mfaEnabled
                      ? null
                      : FilledButton.tonal(
                          onPressed: _enrollMfa,
                          child: const Text('Enable'),
                        ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _MfaSetupSheet extends ConsumerStatefulWidget {
  const _MfaSetupSheet({required this.setup});

  final ResellerMfaSetup setup;

  @override
  ConsumerState<_MfaSetupSheet> createState() => _MfaSetupSheetState();
}

class _MfaSetupSheetState extends ConsumerState<_MfaSetupSheet> {
  final _code = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _code.dispose();
    super.dispose();
  }

  Future<void> _confirm() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(resellerRepositoryProvider).mfaConfirm(
            methodId: widget.setup.methodId,
            code: _code.text.trim(),
          );
      if (mounted) Navigator.of(context).pop(true);
    } catch (_) {
      setState(() {
        _busy = false;
        _error = 'Invalid code — check your authenticator app and try again.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: EdgeInsets.only(
        left: 16,
        right: 16,
        top: 16,
        bottom: 16 + MediaQuery.of(context).viewInsets.bottom,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Set up two-factor authentication',
              style: theme.textTheme.titleMedium),
          const SizedBox(height: 8),
          const Text(
              '1. Add this secret to your authenticator app (or open the '
              'link).\n2. Enter the 6-digit code it shows.'),
          const SizedBox(height: 12),
          Card(
            child: ListTile(
              dense: true,
              title: Text(widget.setup.secret,
                  style: const TextStyle(fontFamily: 'monospace')),
              trailing: IconButton(
                icon: const Icon(Icons.copy, size: 18),
                tooltip: 'Copy secret',
                onPressed: () =>
                    Clipboard.setData(ClipboardData(text: widget.setup.secret)),
              ),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _code,
            keyboardType: TextInputType.number,
            maxLength: 6,
            decoration: InputDecoration(
              labelText: 'Verification code',
              border: const OutlineInputBorder(),
              errorText: _error,
              counterText: '',
            ),
          ),
          const SizedBox(height: 12),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              TextButton(
                onPressed:
                    _busy ? null : () => Navigator.of(context).pop(false),
                child: const Text('Cancel'),
              ),
              const SizedBox(width: 8),
              FilledButton(
                onPressed: _busy ? null : _confirm,
                child: Text(_busy ? 'Verifying…' : 'Verify & enable'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
