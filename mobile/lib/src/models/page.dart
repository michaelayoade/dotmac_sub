/// Mirrors `ListResponse[T]` from app/schemas/common.py:
///   { "items": [...], "count": int, "limit": int, "offset": int }
class Page<T> {
  Page({
    required this.items,
    required this.count,
    required this.limit,
    required this.offset,
  });

  final List<T> items;
  final int count;
  final int limit;
  final int offset;

  bool get hasMore => offset + items.length < count;

  factory Page.fromJson(
    Map<String, dynamic> json,
    T Function(Map<String, dynamic>) itemFromJson,
  ) {
    final rawItems = (json['items'] as List? ?? const [])
        .cast<Map<String, dynamic>>()
        .map(itemFromJson)
        .toList();
    return Page(
      items: rawItems,
      count: (json['count'] as num?)?.toInt() ?? rawItems.length,
      limit: (json['limit'] as num?)?.toInt() ?? rawItems.length,
      offset: (json['offset'] as num?)?.toInt() ?? 0,
    );
  }
}
