import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../config/env.dart';
import '../../providers/auth_controller.dart';

/// Shown when a valid session is held behind the biometric app-lock. Prompts
/// automatically on open; on success the router redirects to the dashboard.
/// "Sign out" is the escape hatch when the user can't satisfy the prompt.
class LockScreen extends ConsumerStatefulWidget {
  const LockScreen({super.key});

  @override
  ConsumerState<LockScreen> createState() => _LockScreenState();
}

class _LockScreenState extends ConsumerState<LockScreen> {
  bool _authenticating = false;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _unlock());
  }

  Future<void> _unlock() async {
    if (_authenticating) return;
    setState(() {
      _authenticating = true;
      _failed = false;
    });
    final ok = await ref.read(authControllerProvider.notifier).unlock();
    if (!mounted) return;
    // On success the router redirect handles navigation; just clear local state.
    setState(() {
      _authenticating = false;
      _failed = !ok;
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  Icons.lock_outline,
                  size: 64,
                  color: theme.colorScheme.primary,
                ),
                const SizedBox(height: 16),
                Text(
                  Brand.name,
                  textAlign: TextAlign.center,
                  style: theme.textTheme.headlineSmall,
                ),
                const SizedBox(height: 4),
                Text(
                  _failed
                      ? 'Authentication failed. Try again.'
                      : 'Unlock to continue',
                  textAlign: TextAlign.center,
                  style: theme.textTheme.bodyMedium?.copyWith(
                    color: _failed
                        ? theme.colorScheme.error
                        : theme.colorScheme.onSurfaceVariant,
                  ),
                ),
                const SizedBox(height: 32),
                FilledButton.icon(
                  onPressed: _authenticating ? null : _unlock,
                  icon: _authenticating
                      ? const SizedBox(
                          height: 20,
                          width: 20,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.fingerprint),
                  label: const Text('Unlock'),
                ),
                TextButton(
                  onPressed: _authenticating
                      ? null
                      : () =>
                            ref.read(authControllerProvider.notifier).logout(),
                  child: const Text('Sign out instead'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
