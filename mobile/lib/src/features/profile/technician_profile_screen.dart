import 'dart:async';
import 'dart:typed_data';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../models/field_identity.dart';
import '../../providers/field_identity_provider.dart';

/// Online-only disclosure of the actual reviewed person assigned to this visit.
class TechnicianProfileScreen extends ConsumerStatefulWidget {
  const TechnicianProfileScreen({super.key, required this.workOrderId});
  final String workOrderId;
  @override
  ConsumerState<TechnicianProfileScreen> createState() =>
      _TechnicianProfileScreenState();
}

class _TechnicianProfileScreenState
    extends ConsumerState<TechnicianProfileScreen> with WidgetsBindingObserver {
  Timer? _poll;
  Animation<double>? _coverAnimation;
  ApprovedFieldIdentity? _identity;
  Uint8List? _portrait;
  FieldIdentityUnavailable? _unavailable;
  bool _loading = true;
  bool _error = false;
  bool _foreground = true;
  int _request = 0;

  bool get _visible =>
      _foreground && (ModalRoute.of(context)?.isCurrent ?? false);
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _foreground = WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && _visible) _load();
    });
    _poll = Timer.periodic(const Duration(seconds: 20), (_) {
      if (mounted && _visible && !_loading) _load();
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final animation = ModalRoute.of(context)?.secondaryAnimation;
    if (_coverAnimation != animation) {
      _coverAnimation?.removeStatusListener(_routeChanged);
      _coverAnimation = animation;
      animation?.addStatusListener(_routeChanged);
    }
  }

  void _routeChanged(AnimationStatus status) {
    if (status == AnimationStatus.forward) {
      _clear();
    }
    if (status == AnimationStatus.dismissed && mounted) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && _visible) _load();
      });
    }
  }

  void _clear() {
    _request++;
    final old = _portrait;
    if (old != null) unawaited(MemoryImage(old).evict());
    if (mounted) {
      setState(() {
        _identity = null;
        _portrait = null;
        _unavailable = null;
        _loading = true;
        _error = false;
      });
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _clear();
    if (_foreground && mounted && _visible) _load();
  }

  Future<void> _load() async {
    if (!mounted || !_visible) return;
    _clear();
    final request = _request;
    try {
      final repository = ref.read(fieldIdentityRepositoryProvider);
      final result = await repository.card(widget.workOrderId);
      if (!mounted || request != _request || !_visible) return;
      final identity = result.approved;
      if (identity == null) {
        setState(() {
          _loading = false;
          _unavailable = result.unavailable;
        });
        return;
      }
      final bytes = await repository.portrait(widget.workOrderId, identity);
      if (!mounted || request != _request || !_visible) return;
      final codec = await ui.instantiateImageCodec(bytes);
      try {
        final frame = await codec.getNextFrame();
        frame.image.dispose();
      } finally {
        codec.dispose();
      }
      if (!mounted || request != _request || !_visible) return;
      // Recheck after portrait retrieval; do not pair old bytes with a new card.
      final confirmed = await repository.card(widget.workOrderId);
      if (!mounted || request != _request || !_visible) return;
      if (confirmed.approved == null ||
          !identity.sameRevision(confirmed.approved!)) {
        throw const FormatException('Assignment changed');
      }
      setState(() {
        _identity = identity;
        _portrait = bytes;
        _loading = false;
      });
    } catch (_) {
      if (!mounted || request != _request || !_visible) return;
      setState(() {
        _identity = null;
        _portrait = null;
        _loading = false;
        _error = true;
      });
    }
  }

  @override
  void dispose() {
    _request++;
    _poll?.cancel();
    _coverAnimation?.removeStatusListener(_routeChanged);
    WidgetsBinding.instance.removeObserver(this);
    final old = _portrait;
    if (old != null) unawaited(MemoryImage(old).evict());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final identity = _identity;
    final photo = _portrait;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Your technician'),
        actions: [
          IconButton(
            onPressed: _loading ? null : _load,
            tooltip: 'Refresh technician',
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: _loading
              ? const CircularProgressIndicator()
              : identity != null && photo != null
                  ? Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Image.memory(
                          photo,
                          key: ValueKey(
                            '${identity.assignmentGeneration}:${identity.portraitRevision}',
                          ),
                          width: 180,
                          height: 180,
                          fit: BoxFit.cover,
                          gaplessPlayback: false,
                          errorBuilder: (_, __, ___) =>
                              const Icon(Icons.person_outline, size: 100),
                        ),
                        const SizedBox(height: 20),
                        Text(
                          identity.displayName,
                          style: Theme.of(context).textTheme.headlineSmall,
                        ),
                        Text(identity.companyName),
                        const SizedBox(height: 12),
                        const Chip(
                          avatar: Icon(Icons.verified_outlined),
                          label: Text('Identity reviewed'),
                        ),
                      ],
                    )
                  : Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          _error
                              ? 'We could not confirm your technician. Connect and try again.'
                              : switch (_unavailable) {
                                  FieldIdentityUnavailable.crewNotSelected =>
                                    'The service company has not selected your visiting crew yet.',
                                  FieldIdentityUnavailable.unassigned =>
                                    'A technician has not been assigned to this visit yet.',
                                  FieldIdentityUnavailable.notApproved =>
                                    'A reviewed technician profile is not available for this visit yet.',
                                  _ =>
                                    'Technician identity is not available for this visit.',
                                },
                          textAlign: TextAlign.center,
                        ),
                        const SizedBox(height: 16),
                        FilledButton(
                            onPressed: _load, child: const Text('Retry')),
                      ],
                    ),
        ),
      ),
    );
  }
}
