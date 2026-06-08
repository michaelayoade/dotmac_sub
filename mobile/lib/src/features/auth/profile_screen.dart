import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../config/env.dart';
import '../../core/api_exception.dart';
import '../../models/auth.dart';
import '../../providers/auth_controller.dart';
import '../billing/payment_methods_screen.dart';

class ProfileScreen extends ConsumerWidget {
  const ProfileScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final me = ref.watch(currentUserProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Profile'),
        actions: [
          if (me != null)
            IconButton(
              icon: const Icon(Icons.edit_outlined),
              tooltip: 'Edit profile',
              onPressed: () => showModalBottomSheet<void>(
                context: context,
                isScrollControlled: true,
                builder: (_) => _EditProfileSheet(me: me),
              ),
            ),
        ],
      ),
      body: me == null
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Center(child: _AvatarEditor(me: me)),
                const SizedBox(height: 12),
                Center(
                  child: Text(me.fullName,
                      style: Theme.of(context).textTheme.titleLarge),
                ),
                Center(child: Text(me.email)),
                const SizedBox(height: 24),
                Card(
                  child: Column(
                    children: [
                      _Tile(label: 'Phone', value: me.phone ?? '—'),
                      const Divider(height: 1),
                      _Tile(
                          label: 'Email verified',
                          value: me.emailVerified ? 'Yes' : 'No'),
                      const Divider(height: 1),
                      _Tile(label: 'Locale', value: me.locale ?? '—'),
                      const Divider(height: 1),
                      _Tile(label: 'Timezone', value: me.timezone ?? '—'),
                      if (me.roles.isNotEmpty) ...[
                        const Divider(height: 1),
                        _Tile(label: 'Roles', value: me.roles.join(', ')),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.password_outlined),
                    title: const Text('Change password'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => _showChangePassword(context, ref),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.credit_card_outlined),
                    title: const Text('Payment methods'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute(
                          builder: (_) => const PaymentMethodsScreen()),
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.devices_outlined),
                    title: const Text('Active sessions'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.go('/profile/sessions'),
                  ),
                ),
                const SizedBox(height: 12),
                const _BiometricToggle(),
                const SizedBox(height: 24),
                FilledButton.tonalIcon(
                  style: FilledButton.styleFrom(
                    backgroundColor:
                        Theme.of(context).colorScheme.errorContainer,
                    foregroundColor:
                        Theme.of(context).colorScheme.onErrorContainer,
                  ),
                  icon: const Icon(Icons.logout),
                  label: const Text('Sign out'),
                  onPressed: () =>
                      ref.read(authControllerProvider.notifier).logout(),
                ),
              ],
            ),
    );
  }

  void _showChangePassword(BuildContext context, WidgetRef ref) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      builder: (_) => const _ChangePasswordSheet(),
    );
  }
}

/// Tappable avatar with an edit badge: pick a photo (gallery) to upload, or
/// remove the current one. Wraps POST/DELETE /auth/me/avatar.
class _AvatarEditor extends ConsumerStatefulWidget {
  const _AvatarEditor({required this.me});
  final Me me;

  @override
  ConsumerState<_AvatarEditor> createState() => _AvatarEditorState();
}

class _AvatarEditorState extends ConsumerState<_AvatarEditor> {
  bool _busy = false;

  Future<void> _pickAndUpload() async {
    final messenger = ScaffoldMessenger.of(context);
    final XFile? picked;
    try {
      picked = await ImagePicker().pickImage(
        source: ImageSource.gallery,
        maxWidth: 1024,
        imageQuality: 85,
      );
    } catch (e) {
      messenger
          .showSnackBar(SnackBar(content: Text('Could not open gallery: $e')));
      return;
    }
    if (picked == null) return;
    setState(() => _busy = true);
    try {
      final bytes = await picked.readAsBytes();
      await ref.read(authRepositoryProvider).uploadAvatar(
            bytes: bytes,
            filename: picked.name,
            contentType: picked.mimeType,
          );
      await ref.read(authControllerProvider.notifier).reloadProfile();
      messenger.showSnackBar(const SnackBar(content: Text('Photo updated')));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('Upload failed: $e')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _remove() async {
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      await ref.read(authRepositoryProvider).deleteAvatar();
      await ref.read(authControllerProvider.notifier).reloadProfile();
      messenger.showSnackBar(const SnackBar(content: Text('Photo removed')));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _openSheet() {
    final hasAvatar = widget.me.avatarUrl != null;
    showModalBottomSheet<void>(
      context: context,
      builder: (_) => SafeArea(
        child: Wrap(
          children: [
            ListTile(
              leading: const Icon(Icons.photo_library_outlined),
              title: const Text('Choose photo'),
              onTap: () {
                Navigator.pop(context);
                _pickAndUpload();
              },
            ),
            if (hasAvatar)
              ListTile(
                leading: const Icon(Icons.delete_outline),
                title: const Text('Remove photo'),
                onTap: () {
                  Navigator.pop(context);
                  _remove();
                },
              ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final me = widget.me;
    return Stack(
      alignment: Alignment.bottomRight,
      children: [
        CircleAvatar(
          radius: 40,
          backgroundImage: me.avatarUrl != null
              ? NetworkImage(Env.resolveUrl(me.avatarUrl!))
              : null,
          child: me.avatarUrl == null
              ? Text(me.initials, style: const TextStyle(fontSize: 28))
              : null,
        ),
        if (_busy)
          const SizedBox(
            width: 80,
            height: 80,
            child: CircleAvatar(
              radius: 40,
              backgroundColor: Colors.black45,
              child: CircularProgressIndicator(color: Colors.white),
            ),
          ),
        Material(
          color: Theme.of(context).colorScheme.primary,
          shape: const CircleBorder(),
          child: InkWell(
            customBorder: const CircleBorder(),
            onTap: _busy ? null : _openSheet,
            child: const Padding(
              padding: EdgeInsets.all(6),
              child: Icon(Icons.edit, size: 16, color: Colors.white),
            ),
          ),
        ),
      ],
    );
  }
}

/// Opt-in toggle for the biometric app-lock. Enabling requires a successful
/// biometric check first (proves the user can satisfy the lock). When the
/// device has no usable biometrics the tile is shown disabled with a reason.
class _BiometricToggle extends ConsumerStatefulWidget {
  const _BiometricToggle();

  @override
  ConsumerState<_BiometricToggle> createState() => _BiometricToggleState();
}

class _BiometricToggleState extends ConsumerState<_BiometricToggle> {
  bool _loading = true;
  bool _available = false;
  bool _enabled = false;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final controller = ref.read(authControllerProvider.notifier);
    final available = await controller.biometricAvailable();
    final enabled = await controller.isBiometricLockEnabled();
    if (!mounted) return;
    setState(() {
      _available = available;
      _enabled = enabled;
      _loading = false;
    });
  }

  Future<void> _toggle(bool value) async {
    final controller = ref.read(authControllerProvider.notifier);
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      if (value) {
        final ok = await controller.enableBiometricLock();
        if (!mounted) return;
        if (ok) {
          setState(() => _enabled = true);
        } else {
          messenger.showSnackBar(const SnackBar(
              content: Text('Could not enable biometric unlock')));
        }
      } else {
        await controller.disableBiometricLock();
        if (!mounted) return;
        setState(() => _enabled = false);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const SizedBox.shrink();
    if (!_available) {
      return const Card(
        child: ListTile(
          enabled: false,
          leading: Icon(Icons.fingerprint),
          title: Text('Biometric unlock'),
          subtitle: Text('Not available on this device'),
        ),
      );
    }
    return Card(
      child: SwitchListTile(
        secondary: const Icon(Icons.fingerprint),
        title: const Text('Biometric unlock'),
        subtitle: const Text('Require Face ID / fingerprint to open the app'),
        value: _enabled,
        onChanged: _busy ? null : _toggle,
      ),
    );
  }
}

class _Tile extends StatelessWidget {
  const _Tile({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return ListTile(
      title: Text(label),
      trailing: Flexible(
        child: Text(value,
            textAlign: TextAlign.end,
            style: TextStyle(color: Theme.of(context).colorScheme.outline)),
      ),
    );
  }
}

/// Edit the customer's contact details (PATCH /auth/me). Email is read-only —
/// it identifies the account and changing it needs verification elsewhere.
class _EditProfileSheet extends ConsumerStatefulWidget {
  const _EditProfileSheet({required this.me});
  final Me me;

  @override
  ConsumerState<_EditProfileSheet> createState() => _EditProfileSheetState();
}

class _EditProfileSheetState extends ConsumerState<_EditProfileSheet> {
  late final _firstName = TextEditingController(text: widget.me.firstName);
  late final _lastName = TextEditingController(text: widget.me.lastName);
  late final _phone = TextEditingController(text: widget.me.phone ?? '');
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _firstName.dispose();
    _lastName.dispose();
    _phone.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(authRepositoryProvider).updateProfile({
        'first_name': _firstName.text.trim(),
        'last_name': _lastName.text.trim(),
        'phone': _phone.text.trim(),
      });
      await ref.read(authControllerProvider.notifier).reloadProfile();
      navigator.pop();
      messenger.showSnackBar(const SnackBar(content: Text('Profile updated')));
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
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Edit profile', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 16),
          if (_error != null) ...[
            Text(_error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error)),
            const SizedBox(height: 8),
          ],
          TextField(
            controller: _firstName,
            textCapitalization: TextCapitalization.words,
            decoration: const InputDecoration(labelText: 'First name'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _lastName,
            textCapitalization: TextCapitalization.words,
            decoration: const InputDecoration(labelText: 'Last name'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _phone,
            keyboardType: TextInputType.phone,
            decoration: const InputDecoration(labelText: 'Phone'),
          ),
          const SizedBox(height: 8),
          Text('Email: ${widget.me.email}',
              style: Theme.of(context).textTheme.bodySmall),
          const SizedBox(height: 20),
          FilledButton(
            onPressed: _busy ? null : _save,
            child: _busy
                ? const SizedBox(
                    height: 20,
                    width: 20,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Save'),
          ),
        ],
      ),
    );
  }
}

class _ChangePasswordSheet extends ConsumerStatefulWidget {
  const _ChangePasswordSheet();

  @override
  ConsumerState<_ChangePasswordSheet> createState() =>
      _ChangePasswordSheetState();
}

class _ChangePasswordSheetState extends ConsumerState<_ChangePasswordSheet> {
  final _current = TextEditingController();
  final _next = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _current.dispose();
    _next.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_next.text.length < 8) {
      setState(() => _error = 'New password must be at least 8 characters');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(authRepositoryProvider).changePassword(
            currentPassword: _current.text,
            newPassword: _next.text,
          );
      if (mounted) {
        Navigator.of(context).pop();
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Password changed')),
        );
      }
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final insets = MediaQuery.of(context).viewInsets.bottom;
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 16, 16, insets + 16),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Change password',
              style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 16),
          if (_error != null) ...[
            Text(_error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error)),
            const SizedBox(height: 8),
          ],
          TextField(
            controller: _current,
            obscureText: true,
            decoration: const InputDecoration(labelText: 'Current password'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _next,
            obscureText: true,
            decoration: const InputDecoration(labelText: 'New password'),
          ),
          const SizedBox(height: 20),
          FilledButton(
            onPressed: _busy ? null : _save,
            child: _busy
                ? const SizedBox(
                    height: 20,
                    width: 20,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Save'),
          ),
        ],
      ),
    );
  }
}
