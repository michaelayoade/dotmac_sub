import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_exception.dart';
import '../../core/semantic_colors.dart';
import '../../models/reseller.dart';
import '../../providers/auth_controller.dart';
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
              const SizedBox(height: 8),
              // Email-verification state comes from /me (same as customers).
              Card(
                child: _ResellerEmailVerifiedTile(
                  verified:
                      ref.watch(currentUserProvider)?.emailVerified ?? false,
                ),
              ),
              const SizedBox(height: 24),
              Text('Account', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              Card(
                child: ListTile(
                  leading: const Icon(Icons.contacts_outlined),
                  title: const Text('Additional contacts'),
                  subtitle:
                      const Text('People we can reach about your account'),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: () => context.push('/reseller/contacts'),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

/// Email-verification status for the reseller, with a self-service re-send
/// (POST /auth/resend-verification-email) — mirrors the customer profile tile.
class _ResellerEmailVerifiedTile extends ConsumerStatefulWidget {
  const _ResellerEmailVerifiedTile({required this.verified});
  final bool verified;

  @override
  ConsumerState<_ResellerEmailVerifiedTile> createState() =>
      _ResellerEmailVerifiedTileState();
}

class _ResellerEmailVerifiedTileState
    extends ConsumerState<_ResellerEmailVerifiedTile> {
  bool _busy = false;

  Future<void> _resend() async {
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      final sent =
          await ref.read(authRepositoryProvider).resendVerificationEmail();
      if (sent) {
        messenger.showSnackBar(const SnackBar(
            content: Text('Verification email sent — check your inbox.')));
        // The verified flag lives on /me; refresh in case it just flipped.
        await ref.read(authControllerProvider.notifier).reloadProfile();
      } else {
        messenger.showSnackBar(const SnackBar(
            content: Text('Already verified or no email on file.')));
      }
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(
        content: Text(e.statusCode == 429
            ? 'Please wait a bit before trying again.'
            : e.message),
      ));
    } catch (_) {
      messenger.showSnackBar(const SnackBar(
          content: Text('Could not send verification email. Try again.')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    if (widget.verified) {
      return ListTile(
        title: const Text('Email verified'),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.verified, size: 18, color: context.semantic.success),
            const SizedBox(width: 6),
            const Text('Verified'),
          ],
        ),
      );
    }
    return ListTile(
      title: const Text('Email verified'),
      subtitle: const Text('Check your inbox for the verification link.'),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.error_outline, size: 18, color: scheme.error),
          const SizedBox(width: 6),
          _busy
              ? const SizedBox(
                  height: 16,
                  width: 16,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : TextButton(
                  onPressed: _resend,
                  child: const Text('Resend'),
                ),
        ],
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
