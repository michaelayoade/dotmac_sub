import 'dart:io';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

/// Maximum number of attachments per ticket / comment (frozen server contract).
const int kMaxAttachments = 5;

/// Maximum size of a single attachment in bytes (5 MB, frozen server contract).
const int kMaxAttachmentBytes = 5 * 1024 * 1024;

/// A picked attachment plus its on-disk size (so we can validate ≤5 MB and
/// render a thumbnail).
class PickedAttachment {
  PickedAttachment({required this.file, required this.bytes});

  final XFile file;
  final int bytes;

  String get name => file.name;
  String get path => file.path;
  bool get isImage {
    final mt = file.mimeType ?? '';
    if (mt.startsWith('image/')) return true;
    final ext = name.toLowerCase().split('.').last;
    return const {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic'}.contains(ext);
  }
}

/// Reusable "Attach" affordance: offers **Take photo** (camera) and **Choose
/// from gallery**, enforces ≤5 files / ≤5 MB each client-side, and shows the
/// selected items as removable thumbnails/filename chips.
///
/// Image-only: the app has no file_picker dependency, so PDFs can't be picked
/// here (the repository/multipart contract still accepts them when supplied).
class AttachmentPicker extends StatelessWidget {
  const AttachmentPicker({
    super.key,
    required this.attachments,
    required this.onChanged,
    this.enabled = true,
  });

  final List<PickedAttachment> attachments;
  final ValueChanged<List<PickedAttachment>> onChanged;
  final bool enabled;

  /// Prompt for a source (camera/gallery), validate (≤5 files, ≤5 MB), and
  /// return the new attachment list — or null if the user cancelled or the pick
  /// was rejected. Shared by the inline button and any external "attach" entry.
  static Future<List<PickedAttachment>?> pickInto(
    BuildContext context,
    List<PickedAttachment> current,
  ) async {
    final messenger = ScaffoldMessenger.of(context);
    if (current.length >= kMaxAttachments) {
      messenger.showSnackBar(
        const SnackBar(content: Text('You can attach up to 5 files.')),
      );
      return null;
    }
    final source = await showModalBottomSheet<ImageSource>(
      context: context,
      builder: (_) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.photo_camera_outlined),
              title: const Text('Take photo'),
              onTap: () => Navigator.pop(context, ImageSource.camera),
            ),
            ListTile(
              leading: const Icon(Icons.photo_library_outlined),
              title: const Text('Choose from gallery'),
              onTap: () => Navigator.pop(context, ImageSource.gallery),
            ),
          ],
        ),
      ),
    );
    if (source == null) return null;
    final XFile? picked;
    try {
      picked = await ImagePicker().pickImage(source: source, imageQuality: 85);
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('Could not open: $e')));
      return null;
    }
    if (picked == null) return null;
    final bytes = await picked.length();
    if (bytes > kMaxAttachmentBytes) {
      messenger.showSnackBar(
        const SnackBar(content: Text('Each file must be 5 MB or smaller.')),
      );
      return null;
    }
    return [...current, PickedAttachment(file: picked, bytes: bytes)];
  }

  Future<void> _openSheet(BuildContext context) async {
    final next = await pickInto(context, attachments);
    if (next != null) onChanged(next);
  }

  void _remove(PickedAttachment a) =>
      onChanged(attachments.where((x) => x != a).toList());

  @override
  Widget build(BuildContext context) {
    final canAdd = enabled && attachments.length < kMaxAttachments;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        OutlinedButton.icon(
          onPressed: canAdd ? () => _openSheet(context) : null,
          icon: const Icon(Icons.attach_file, size: 18),
          label: Text(
            attachments.isEmpty
                ? 'Attach photo'
                : 'Add photo (${attachments.length}/$kMaxAttachments)',
          ),
        ),
        if (attachments.isNotEmpty) ...[
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final a in attachments)
                _AttachmentThumb(
                  attachment: a,
                  onRemove: enabled ? () => _remove(a) : null,
                ),
            ],
          ),
        ],
      ],
    );
  }
}

class _AttachmentThumb extends StatelessWidget {
  const _AttachmentThumb({required this.attachment, this.onRemove});

  final PickedAttachment attachment;
  final VoidCallback? onRemove;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Stack(
      clipBehavior: Clip.none,
      children: [
        Container(
          width: 72,
          height: 72,
          decoration: BoxDecoration(
            color: scheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: scheme.outlineVariant),
          ),
          clipBehavior: Clip.antiAlias,
          child: attachment.isImage
              ? Image.file(File(attachment.path), fit: BoxFit.cover)
              : Center(
                  child: Padding(
                    padding: const EdgeInsets.all(4),
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        const Icon(Icons.insert_drive_file_outlined, size: 24),
                        Text(
                          attachment.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: Theme.of(context).textTheme.labelSmall,
                        ),
                      ],
                    ),
                  ),
                ),
        ),
        if (onRemove != null)
          Positioned(
            top: -8,
            right: -8,
            child: Material(
              color: scheme.error,
              shape: const CircleBorder(),
              child: InkWell(
                customBorder: const CircleBorder(),
                onTap: onRemove,
                child: Padding(
                  padding: const EdgeInsets.all(2),
                  child: Icon(Icons.close, size: 16, color: scheme.onError),
                ),
              ),
            ),
          ),
      ],
    );
  }
}
