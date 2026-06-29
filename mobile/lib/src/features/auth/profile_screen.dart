import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../config/env.dart';
import '../../core/api_exception.dart';
import '../../core/semantic_colors.dart';
import '../../models/auth.dart';
import '../../providers/auth_controller.dart';

class ProfileScreen extends ConsumerStatefulWidget {
  const ProfileScreen({super.key});

  @override
  ConsumerState<ProfileScreen> createState() => _ProfileScreenState();
}

class _ProfileScreenState extends ConsumerState<ProfileScreen> {
  @override
  void initState() {
    super.initState();
    // Refresh /auth/me when the screen opens so out-of-band changes (e.g. email
    // verified via the email link) reflect immediately instead of the
    // login-time snapshot. Post-frame so it runs after the first build.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) ref.read(authControllerProvider.notifier).reloadProfile();
    });
  }

  @override
  Widget build(BuildContext context) {
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
                  child: Text(
                    me.fullName,
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                ),
                Center(child: Text(me.email)),
                const SizedBox(height: 24),
                Card(
                  child: Column(
                    // Hide rows the backend has no value for rather than showing
                    // a row of dashes; dividers are inserted between whatever
                    // rows actually render.
                    children: _withDividers([
                      _Tile(label: 'Phone', value: me.phone ?? '—'),
                      _EmailVerifiedTile(verified: me.emailVerified),
                      if (me.locale != null && me.locale!.isNotEmpty)
                        _Tile(label: 'Locale', value: me.locale!),
                      if (me.timezone != null && me.timezone!.isNotEmpty)
                        _Tile(label: 'Timezone', value: me.timezone!),
                      if (me.roles.isNotEmpty)
                        _Tile(label: 'Roles', value: me.roles.join(', ')),
                    ]),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.password_outlined),
                    title: const Text('Change password'),
                    subtitle: const Text('Your app & portal sign-in password'),
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
                    onTap: () => context.push('/profile/payment-methods'),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.place_outlined),
                    title: const Text('Service location'),
                    subtitle: const Text('Check & correct your map pin'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.push('/profile/service-location'),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.contacts_outlined),
                    title: const Text('Additional contacts'),
                    subtitle: const Text(
                      'People we can reach about your account',
                    ),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.push('/profile/contacts'),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.timeline_outlined),
                    title: const Text('Installation progress'),
                    subtitle:
                        const Text('Track your install, survey to activation'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.push('/profile/installation-progress'),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.card_giftcard_outlined),
                    title: const Text('Refer & Earn'),
                    subtitle: const Text('Invite friends, earn wallet credit'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.push('/profile/refer-and-earn'),
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
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.settings_outlined),
                    title: const Text('Settings'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.push('/profile/settings'),
                  ),
                ),
                const SizedBox(height: 12),
                const _BiometricToggle(),
                const SizedBox(height: 24),
                FilledButton.tonalIcon(
                  style: FilledButton.styleFrom(
                    backgroundColor: Theme.of(
                      context,
                    ).colorScheme.errorContainer,
                    foregroundColor: Theme.of(
                      context,
                    ).colorScheme.onErrorContainer,
                  ),
                  icon: const Icon(Icons.logout),
                  label: const Text('Sign out'),
                  onPressed: () =>
                      ref.read(authControllerProvider.notifier).logout(),
                ),
                const SizedBox(height: 8),
                TextButton.icon(
                  style: TextButton.styleFrom(
                    foregroundColor: Theme.of(context).colorScheme.error,
                  ),
                  icon: const Icon(Icons.delete_forever_outlined, size: 18),
                  label: const Text('Delete account'),
                  onPressed: () => _confirmDeleteAccount(context, ref),
                ),
              ],
            ),
    );
  }

  Future<void> _confirmDeleteAccount(
    BuildContext context,
    WidgetRef ref,
  ) async {
    final messenger = ScaffoldMessenger.of(context);
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete account?'),
        content: const Text(
          'This closes your DotMac account and signs you out. Your service will '
          'end and your personal data will be deleted per our privacy policy '
          '(some billing and tax records are kept where the law requires). '
          'To restore your account afterwards, contact support@dotmac.ng.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Delete account'),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    try {
      await ref.read(authControllerProvider.notifier).deleteAccount();
      messenger.showSnackBar(
        const SnackBar(
          content: Text(
            'Your account has been closed. You have been signed out.',
          ),
        ),
      );
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } catch (_) {
      messenger.showSnackBar(
        const SnackBar(
          content: Text(
            'Could not delete your account — please try again or '
            'contact support@dotmac.ng.',
          ),
        ),
      );
    }
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
      messenger.showSnackBar(
        SnackBar(content: Text('Could not open gallery: $e')),
      );
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
          messenger.showSnackBar(
            const SnackBar(content: Text('Could not enable biometric unlock')),
          );
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
      // Availability can change mid-session (the user enrolls a fingerprint in
      // OS settings) and this tile stays alive in the IndexedStack shell, so
      // let a tap re-check rather than caching the verdict forever.
      return Card(
        child: ListTile(
          leading: const Icon(Icons.fingerprint),
          title: const Text('Biometric unlock'),
          subtitle: const Text(
            'Not available on this device. Tap to check again.',
          ),
          onTap: _load,
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

/// Interleaves a 1px divider between each visible row.
List<Widget> _withDividers(List<Widget> rows) {
  final out = <Widget>[];
  for (var i = 0; i < rows.length; i++) {
    if (i > 0) out.add(const Divider(height: 1));
    out.add(rows[i]);
  }
  return out;
}

/// Email-verification status, drawn prominently when unverified so the customer
/// notices and knows to act. When unverified it offers a self-service re-send
/// (POST /auth/resend-verification-email).
class _EmailVerifiedTile extends ConsumerStatefulWidget {
  const _EmailVerifiedTile({required this.verified});
  final bool verified;

  @override
  ConsumerState<_EmailVerifiedTile> createState() => _EmailVerifiedTileState();
}

class _EmailVerifiedTileState extends ConsumerState<_EmailVerifiedTile> {
  bool _busy = false;

  Future<void> _resend() async {
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busy = true);
    try {
      final sent =
          await ref.read(authRepositoryProvider).resendVerificationEmail();
      if (sent) {
        messenger.showSnackBar(
          const SnackBar(
            content: Text('Verification email sent — check your inbox.'),
          ),
        );
        // The verified flag lives on /me; refresh in case it just flipped.
        await ref.read(authControllerProvider.notifier).reloadProfile();
      } else {
        messenger.showSnackBar(
          const SnackBar(
            content: Text('Already verified or no email on file.'),
          ),
        );
      }
    } on ApiException catch (e) {
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            e.statusCode == 429
                ? 'Please wait a bit before trying again.'
                : e.message,
          ),
        ),
      );
    } catch (_) {
      messenger.showSnackBar(
        const SnackBar(
          content: Text('Could not send verification email. Try again.'),
        ),
      );
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
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : TextButton(onPressed: _resend, child: const Text('Resend')),
        ],
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
      title: Row(
        children: [
          Text(label),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              value,
              textAlign: TextAlign.end,
              style: TextStyle(color: Theme.of(context).colorScheme.outline),
            ),
          ),
        ],
      ),
    );
  }
}

/// Edit the customer's contact details (PATCH /auth/me). Email is editable:
/// changing it server-side resets verified state and sends a fresh
/// verification link; a duplicate address comes back as HTTP 409.
class _EditProfileSheet extends ConsumerStatefulWidget {
  const _EditProfileSheet({required this.me});
  final Me me;

  @override
  ConsumerState<_EditProfileSheet> createState() => _EditProfileSheetState();
}

class _EditProfileSheetState extends ConsumerState<_EditProfileSheet> {
  late final _firstName = TextEditingController(text: widget.me.firstName);
  late final _lastName = TextEditingController(text: widget.me.lastName);
  late final _displayName = TextEditingController(
    text: widget.me.displayName ?? '',
  );
  late final _phone = TextEditingController(text: widget.me.phone ?? '');
  late final _email = TextEditingController(text: widget.me.email);
  late final _addr1 = TextEditingController(text: widget.me.addressLine1 ?? '');
  late final _addr2 = TextEditingController(text: widget.me.addressLine2 ?? '');
  late final _city = TextEditingController(text: widget.me.city ?? '');
  late final _region = TextEditingController(text: widget.me.region ?? '');
  late final _postal = TextEditingController(text: widget.me.postalCode ?? '');
  late final _country = TextEditingController(
    text: widget.me.countryCode ?? '',
  );
  late String _gender = widget.me.gender ?? 'unknown';
  late String? _contactMethod = widget.me.preferredContactMethod;
  late String? _dob = widget.me.dateOfBirth; // ISO yyyy-MM-dd
  bool _busy = false;
  String? _error;

  static const _genders = <String, String>{
    'unknown': 'Prefer not to say',
    'female': 'Female',
    'male': 'Male',
    'non_binary': 'Non-binary',
    'other': 'Other',
  };
  static const _contactMethods = <String, String>{
    'email': 'Email',
    'phone': 'Phone',
    'sms': 'SMS',
    'push': 'Push notification',
  };

  @override
  void dispose() {
    _firstName.dispose();
    _lastName.dispose();
    _displayName.dispose();
    _phone.dispose();
    _email.dispose();
    _addr1.dispose();
    _addr2.dispose();
    _city.dispose();
    _region.dispose();
    _postal.dispose();
    _country.dispose();
    super.dispose();
  }

  Future<void> _pickDob() async {
    final now = DateTime.now();
    DateTime initial = DateTime(now.year - 25);
    if (_dob != null) {
      final parsed = DateTime.tryParse(_dob!);
      if (parsed != null) initial = parsed;
    }
    final picked = await showDatePicker(
      context: context,
      initialDate: initial,
      firstDate: DateTime(1900),
      lastDate: now,
    );
    if (picked != null) {
      setState(
        () => _dob = '${picked.year.toString().padLeft(4, '0')}-'
            '${picked.month.toString().padLeft(2, '0')}-'
            '${picked.day.toString().padLeft(2, '0')}',
      );
    }
  }

  // Loose RFC-ish check: a single @ with non-empty local part and a dotted
  // domain. Server does the authoritative validation.
  static final _emailRe = RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$');

  String? _validateEmail(String value) {
    if (value.isEmpty) return 'Enter an email address';
    if (!_emailRe.hasMatch(value)) return 'Enter a valid email address';
    return null;
  }

  Future<void> _save() async {
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    final email = _email.text.trim();
    final emailError = _validateEmail(email);
    if (emailError != null) {
      setState(() => _error = emailError);
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(authRepositoryProvider).updateProfile({
        'first_name': _firstName.text.trim(),
        'last_name': _lastName.text.trim(),
        'display_name': _displayName.text.trim(),
        'phone': _phone.text.trim(),
        'email': email,
        'date_of_birth': _dob,
        'gender': _gender,
        'preferred_contact_method': _contactMethod ?? '',
        'address_line1': _addr1.text.trim(),
        'address_line2': _addr2.text.trim(),
        'city': _city.text.trim(),
        'region': _region.text.trim(),
        'postal_code': _postal.text.trim(),
        'country_code': _country.text.trim(),
      });
      final emailChanged = email != widget.me.email.trim();
      await ref.read(authControllerProvider.notifier).reloadProfile();
      navigator.pop();
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            emailChanged
                ? 'Email updated — check your inbox to verify it.'
                : 'Profile updated',
          ),
        ),
      );
    } on ApiException catch (e) {
      setState(
        () => _error =
            e.statusCode == 409 ? 'That email is already in use.' : e.message,
      );
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
            Text('Edit profile', style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 16),
            if (_error != null) ...[
              Text(
                _error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
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
              controller: _displayName,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(
                labelText: 'Display name (optional)',
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _phone,
              keyboardType: TextInputType.phone,
              decoration: const InputDecoration(labelText: 'Phone'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _email,
              keyboardType: TextInputType.emailAddress,
              autocorrect: false,
              decoration: const InputDecoration(
                labelText: 'Email',
                helperText: 'Changing this sends a new verification link.',
              ),
            ),
            const SizedBox(height: 12),
            InkWell(
              onTap: _busy ? null : _pickDob,
              child: InputDecorator(
                decoration: const InputDecoration(labelText: 'Date of birth'),
                child: Text(_dob ?? 'Not set'),
              ),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: _gender,
              decoration: const InputDecoration(labelText: 'Gender'),
              items: [
                for (final e in _genders.entries)
                  DropdownMenuItem(value: e.key, child: Text(e.value)),
              ],
              onChanged: _busy
                  ? null
                  : (v) => setState(() => _gender = v ?? 'unknown'),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String?>(
              initialValue: _contactMethod,
              decoration: const InputDecoration(
                labelText: 'Preferred contact method',
              ),
              items: [
                const DropdownMenuItem<String?>(
                  value: null,
                  child: Text('No preference'),
                ),
                for (final e in _contactMethods.entries)
                  DropdownMenuItem<String?>(value: e.key, child: Text(e.value)),
              ],
              onChanged:
                  _busy ? null : (v) => setState(() => _contactMethod = v),
            ),
            const SizedBox(height: 20),
            Text(
              'Contact address',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _addr1,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(labelText: 'Address line 1'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _addr2,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(
                labelText: 'Address line 2 (optional)',
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _city,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(labelText: 'City'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _region,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(labelText: 'State / Region'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _postal,
              decoration: const InputDecoration(labelText: 'Postal code'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _country,
              textCapitalization: TextCapitalization.characters,
              maxLength: 2,
              decoration: const InputDecoration(
                labelText: 'Country',
                helperText: '2-letter code, e.g. NG',
              ),
            ),
            const SizedBox(height: 20),
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
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('Password changed')));
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
          Text(
            'Change password',
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: 4),
          Text(
            'This updates the password you use to sign in to the app and '
            'customer portal. Your internet (PPPoE) connection password is '
            'not affected.',
            style: Theme.of(context).textTheme.bodySmall,
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
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('Save'),
          ),
        ],
      ),
    );
  }
}
