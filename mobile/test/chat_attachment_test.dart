import 'package:dotmac_portal/src/models/chat.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('photo reference and retry upload stay with their chat messages', () {
    final message = ChatMessage.fromHistory({
      'id': 'message-1',
      'body': '',
      'direction': 'inbound',
      'client_message_id': 'photo-1',
      'attachments': [
        {'id': 'asset-1', 'file_name': 'router.jpg'},
      ],
    });
    expect(message.attachments.single.id, 'asset-1');
    expect(message.clientMessageId, 'photo-1');

    const upload = ChatUpload(
      path: '/tmp/router.jpg',
      name: 'router.jpg',
      mimeType: 'image/jpeg',
    );
    final failed = ChatMessage(
      id: 'temp-1',
      body: '',
      fromAgent: false,
      uploads: const [upload],
      clientMessageId: 'photo-1',
    ).copyWith(status: MessageStatus.failed);
    expect(failed.uploads.single, same(upload));
    expect(failed.clientMessageId, 'photo-1');
  });
}
