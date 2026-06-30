import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../config/env.dart';
import '../../providers/auth_controller.dart';

/// Renders nothing — drives the one-time "Sign in with Face ID / fingerprint?"
/// enrollment offer shown right after the first authenticated landing.
///
/// Lives on the home screen (not the login screen) because the router redirects
/// away from login the instant the session becomes authenticated, disposing it
/// before a dialog could show. Asks at most once per device (biometricPromptSeen)
/// and only when biometrics are usable and not already enabled.
class BiometricEnrollmentPrompt extends ConsumerStatefulWidget {
  const BiometricEnrollmentPrompt({super.key});

  @override
  ConsumerState<BiometricEnrollmentPrompt> createState() =>
      _BiometricEnrollmentPromptState();
}

class _BiometricEnrollmentPromptState
    extends ConsumerState<BiometricEnrollmentPrompt> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _maybeOffer());
  }

  Future<void> _maybeOffer() async {
    final auth = ref.read(authControllerProvider.notifier);
    if (await auth.biometricPromptSeen()) return;
    if (await auth.isBiometricLockEnabled()) return;
    if (!await auth.biometricAvailable()) return;
    await auth.markBiometricPromptSeen();
    if (!mounted) return;

    final enable = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        icon: const Icon(Icons.fingerprint, size: 32),
        title: const Text('Sign in faster next time'),
        content: const Text(
          'Use Face ID or your fingerprint to sign in to ${Brand.name} '
          'instead of typing your password.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Not now'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Enable'),
          ),
        ],
      ),
    );
    if (enable != true) return;

    final ok = await auth.enableBiometricLock();
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          ok
              ? 'Face ID / fingerprint sign-in enabled'
              : "Couldn't enable biometric sign-in",
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => const SizedBox.shrink();
}
