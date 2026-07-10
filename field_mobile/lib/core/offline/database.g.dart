// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'database.dart';

// ignore_for_file: type=lint
class $CachedJobsTable extends CachedJobs
    with TableInfo<$CachedJobsTable, CachedJob> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $CachedJobsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _idMeta = const VerificationMeta('id');
  @override
  late final GeneratedColumn<String> id = GeneratedColumn<String>(
    'id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _titleMeta = const VerificationMeta('title');
  @override
  late final GeneratedColumn<String> title = GeneratedColumn<String>(
    'title',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _statusMeta = const VerificationMeta('status');
  @override
  late final GeneratedColumn<String> status = GeneratedColumn<String>(
    'status',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _workTypeMeta = const VerificationMeta(
    'workType',
  );
  @override
  late final GeneratedColumn<String> workType = GeneratedColumn<String>(
    'work_type',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _priorityMeta = const VerificationMeta(
    'priority',
  );
  @override
  late final GeneratedColumn<String> priority = GeneratedColumn<String>(
    'priority',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _scheduledStartMeta = const VerificationMeta(
    'scheduledStart',
  );
  @override
  late final GeneratedColumn<DateTime> scheduledStart =
      GeneratedColumn<DateTime>(
        'scheduled_start',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _detailJsonMeta = const VerificationMeta(
    'detailJson',
  );
  @override
  late final GeneratedColumn<String> detailJson = GeneratedColumn<String>(
    'detail_json',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _cachedAtMeta = const VerificationMeta(
    'cachedAt',
  );
  @override
  late final GeneratedColumn<DateTime> cachedAt = GeneratedColumn<DateTime>(
    'cached_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [
    id,
    title,
    status,
    workType,
    priority,
    scheduledStart,
    detailJson,
    cachedAt,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'cached_jobs';
  @override
  VerificationContext validateIntegrity(
    Insertable<CachedJob> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('id')) {
      context.handle(_idMeta, id.isAcceptableOrUnknown(data['id']!, _idMeta));
    } else if (isInserting) {
      context.missing(_idMeta);
    }
    if (data.containsKey('title')) {
      context.handle(
        _titleMeta,
        title.isAcceptableOrUnknown(data['title']!, _titleMeta),
      );
    } else if (isInserting) {
      context.missing(_titleMeta);
    }
    if (data.containsKey('status')) {
      context.handle(
        _statusMeta,
        status.isAcceptableOrUnknown(data['status']!, _statusMeta),
      );
    } else if (isInserting) {
      context.missing(_statusMeta);
    }
    if (data.containsKey('work_type')) {
      context.handle(
        _workTypeMeta,
        workType.isAcceptableOrUnknown(data['work_type']!, _workTypeMeta),
      );
    } else if (isInserting) {
      context.missing(_workTypeMeta);
    }
    if (data.containsKey('priority')) {
      context.handle(
        _priorityMeta,
        priority.isAcceptableOrUnknown(data['priority']!, _priorityMeta),
      );
    } else if (isInserting) {
      context.missing(_priorityMeta);
    }
    if (data.containsKey('scheduled_start')) {
      context.handle(
        _scheduledStartMeta,
        scheduledStart.isAcceptableOrUnknown(
          data['scheduled_start']!,
          _scheduledStartMeta,
        ),
      );
    }
    if (data.containsKey('detail_json')) {
      context.handle(
        _detailJsonMeta,
        detailJson.isAcceptableOrUnknown(data['detail_json']!, _detailJsonMeta),
      );
    }
    if (data.containsKey('cached_at')) {
      context.handle(
        _cachedAtMeta,
        cachedAt.isAcceptableOrUnknown(data['cached_at']!, _cachedAtMeta),
      );
    } else if (isInserting) {
      context.missing(_cachedAtMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {id};
  @override
  CachedJob map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return CachedJob(
      id: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}id'],
      )!,
      title: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}title'],
      )!,
      status: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}status'],
      )!,
      workType: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}work_type'],
      )!,
      priority: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}priority'],
      )!,
      scheduledStart: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}scheduled_start'],
      ),
      detailJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}detail_json'],
      ),
      cachedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}cached_at'],
      )!,
    );
  }

  @override
  $CachedJobsTable createAlias(String alias) {
    return $CachedJobsTable(attachedDatabase, alias);
  }
}

class CachedJob extends DataClass implements Insertable<CachedJob> {
  final String id;
  final String title;
  final String status;
  final String workType;
  final String priority;
  final DateTime? scheduledStart;
  final String? detailJson;
  final DateTime cachedAt;
  const CachedJob({
    required this.id,
    required this.title,
    required this.status,
    required this.workType,
    required this.priority,
    this.scheduledStart,
    this.detailJson,
    required this.cachedAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['id'] = Variable<String>(id);
    map['title'] = Variable<String>(title);
    map['status'] = Variable<String>(status);
    map['work_type'] = Variable<String>(workType);
    map['priority'] = Variable<String>(priority);
    if (!nullToAbsent || scheduledStart != null) {
      map['scheduled_start'] = Variable<DateTime>(scheduledStart);
    }
    if (!nullToAbsent || detailJson != null) {
      map['detail_json'] = Variable<String>(detailJson);
    }
    map['cached_at'] = Variable<DateTime>(cachedAt);
    return map;
  }

  CachedJobsCompanion toCompanion(bool nullToAbsent) {
    return CachedJobsCompanion(
      id: Value(id),
      title: Value(title),
      status: Value(status),
      workType: Value(workType),
      priority: Value(priority),
      scheduledStart: scheduledStart == null && nullToAbsent
          ? const Value.absent()
          : Value(scheduledStart),
      detailJson: detailJson == null && nullToAbsent
          ? const Value.absent()
          : Value(detailJson),
      cachedAt: Value(cachedAt),
    );
  }

  factory CachedJob.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return CachedJob(
      id: serializer.fromJson<String>(json['id']),
      title: serializer.fromJson<String>(json['title']),
      status: serializer.fromJson<String>(json['status']),
      workType: serializer.fromJson<String>(json['workType']),
      priority: serializer.fromJson<String>(json['priority']),
      scheduledStart: serializer.fromJson<DateTime?>(json['scheduledStart']),
      detailJson: serializer.fromJson<String?>(json['detailJson']),
      cachedAt: serializer.fromJson<DateTime>(json['cachedAt']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'id': serializer.toJson<String>(id),
      'title': serializer.toJson<String>(title),
      'status': serializer.toJson<String>(status),
      'workType': serializer.toJson<String>(workType),
      'priority': serializer.toJson<String>(priority),
      'scheduledStart': serializer.toJson<DateTime?>(scheduledStart),
      'detailJson': serializer.toJson<String?>(detailJson),
      'cachedAt': serializer.toJson<DateTime>(cachedAt),
    };
  }

  CachedJob copyWith({
    String? id,
    String? title,
    String? status,
    String? workType,
    String? priority,
    Value<DateTime?> scheduledStart = const Value.absent(),
    Value<String?> detailJson = const Value.absent(),
    DateTime? cachedAt,
  }) => CachedJob(
    id: id ?? this.id,
    title: title ?? this.title,
    status: status ?? this.status,
    workType: workType ?? this.workType,
    priority: priority ?? this.priority,
    scheduledStart: scheduledStart.present
        ? scheduledStart.value
        : this.scheduledStart,
    detailJson: detailJson.present ? detailJson.value : this.detailJson,
    cachedAt: cachedAt ?? this.cachedAt,
  );
  CachedJob copyWithCompanion(CachedJobsCompanion data) {
    return CachedJob(
      id: data.id.present ? data.id.value : this.id,
      title: data.title.present ? data.title.value : this.title,
      status: data.status.present ? data.status.value : this.status,
      workType: data.workType.present ? data.workType.value : this.workType,
      priority: data.priority.present ? data.priority.value : this.priority,
      scheduledStart: data.scheduledStart.present
          ? data.scheduledStart.value
          : this.scheduledStart,
      detailJson: data.detailJson.present
          ? data.detailJson.value
          : this.detailJson,
      cachedAt: data.cachedAt.present ? data.cachedAt.value : this.cachedAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('CachedJob(')
          ..write('id: $id, ')
          ..write('title: $title, ')
          ..write('status: $status, ')
          ..write('workType: $workType, ')
          ..write('priority: $priority, ')
          ..write('scheduledStart: $scheduledStart, ')
          ..write('detailJson: $detailJson, ')
          ..write('cachedAt: $cachedAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    id,
    title,
    status,
    workType,
    priority,
    scheduledStart,
    detailJson,
    cachedAt,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is CachedJob &&
          other.id == this.id &&
          other.title == this.title &&
          other.status == this.status &&
          other.workType == this.workType &&
          other.priority == this.priority &&
          other.scheduledStart == this.scheduledStart &&
          other.detailJson == this.detailJson &&
          other.cachedAt == this.cachedAt);
}

class CachedJobsCompanion extends UpdateCompanion<CachedJob> {
  final Value<String> id;
  final Value<String> title;
  final Value<String> status;
  final Value<String> workType;
  final Value<String> priority;
  final Value<DateTime?> scheduledStart;
  final Value<String?> detailJson;
  final Value<DateTime> cachedAt;
  final Value<int> rowid;
  const CachedJobsCompanion({
    this.id = const Value.absent(),
    this.title = const Value.absent(),
    this.status = const Value.absent(),
    this.workType = const Value.absent(),
    this.priority = const Value.absent(),
    this.scheduledStart = const Value.absent(),
    this.detailJson = const Value.absent(),
    this.cachedAt = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  CachedJobsCompanion.insert({
    required String id,
    required String title,
    required String status,
    required String workType,
    required String priority,
    this.scheduledStart = const Value.absent(),
    this.detailJson = const Value.absent(),
    required DateTime cachedAt,
    this.rowid = const Value.absent(),
  }) : id = Value(id),
       title = Value(title),
       status = Value(status),
       workType = Value(workType),
       priority = Value(priority),
       cachedAt = Value(cachedAt);
  static Insertable<CachedJob> custom({
    Expression<String>? id,
    Expression<String>? title,
    Expression<String>? status,
    Expression<String>? workType,
    Expression<String>? priority,
    Expression<DateTime>? scheduledStart,
    Expression<String>? detailJson,
    Expression<DateTime>? cachedAt,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (id != null) 'id': id,
      if (title != null) 'title': title,
      if (status != null) 'status': status,
      if (workType != null) 'work_type': workType,
      if (priority != null) 'priority': priority,
      if (scheduledStart != null) 'scheduled_start': scheduledStart,
      if (detailJson != null) 'detail_json': detailJson,
      if (cachedAt != null) 'cached_at': cachedAt,
      if (rowid != null) 'rowid': rowid,
    });
  }

  CachedJobsCompanion copyWith({
    Value<String>? id,
    Value<String>? title,
    Value<String>? status,
    Value<String>? workType,
    Value<String>? priority,
    Value<DateTime?>? scheduledStart,
    Value<String?>? detailJson,
    Value<DateTime>? cachedAt,
    Value<int>? rowid,
  }) {
    return CachedJobsCompanion(
      id: id ?? this.id,
      title: title ?? this.title,
      status: status ?? this.status,
      workType: workType ?? this.workType,
      priority: priority ?? this.priority,
      scheduledStart: scheduledStart ?? this.scheduledStart,
      detailJson: detailJson ?? this.detailJson,
      cachedAt: cachedAt ?? this.cachedAt,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (id.present) {
      map['id'] = Variable<String>(id.value);
    }
    if (title.present) {
      map['title'] = Variable<String>(title.value);
    }
    if (status.present) {
      map['status'] = Variable<String>(status.value);
    }
    if (workType.present) {
      map['work_type'] = Variable<String>(workType.value);
    }
    if (priority.present) {
      map['priority'] = Variable<String>(priority.value);
    }
    if (scheduledStart.present) {
      map['scheduled_start'] = Variable<DateTime>(scheduledStart.value);
    }
    if (detailJson.present) {
      map['detail_json'] = Variable<String>(detailJson.value);
    }
    if (cachedAt.present) {
      map['cached_at'] = Variable<DateTime>(cachedAt.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('CachedJobsCompanion(')
          ..write('id: $id, ')
          ..write('title: $title, ')
          ..write('status: $status, ')
          ..write('workType: $workType, ')
          ..write('priority: $priority, ')
          ..write('scheduledStart: $scheduledStart, ')
          ..write('detailJson: $detailJson, ')
          ..write('cachedAt: $cachedAt, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $CachedScheduleEntriesTable extends CachedScheduleEntries
    with TableInfo<$CachedScheduleEntriesTable, CachedScheduleEntry> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $CachedScheduleEntriesTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _referenceIdMeta = const VerificationMeta(
    'referenceId',
  );
  @override
  late final GeneratedColumn<String> referenceId = GeneratedColumn<String>(
    'reference_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _typeMeta = const VerificationMeta('type');
  @override
  late final GeneratedColumn<String> type = GeneratedColumn<String>(
    'type',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _startAtMeta = const VerificationMeta(
    'startAt',
  );
  @override
  late final GeneratedColumn<DateTime> startAt = GeneratedColumn<DateTime>(
    'start_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _endAtMeta = const VerificationMeta('endAt');
  @override
  late final GeneratedColumn<DateTime> endAt = GeneratedColumn<DateTime>(
    'end_at',
    aliasedName,
    true,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _titleMeta = const VerificationMeta('title');
  @override
  late final GeneratedColumn<String> title = GeneratedColumn<String>(
    'title',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [
    referenceId,
    type,
    startAt,
    endAt,
    title,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'cached_schedule_entries';
  @override
  VerificationContext validateIntegrity(
    Insertable<CachedScheduleEntry> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('reference_id')) {
      context.handle(
        _referenceIdMeta,
        referenceId.isAcceptableOrUnknown(
          data['reference_id']!,
          _referenceIdMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_referenceIdMeta);
    }
    if (data.containsKey('type')) {
      context.handle(
        _typeMeta,
        type.isAcceptableOrUnknown(data['type']!, _typeMeta),
      );
    } else if (isInserting) {
      context.missing(_typeMeta);
    }
    if (data.containsKey('start_at')) {
      context.handle(
        _startAtMeta,
        startAt.isAcceptableOrUnknown(data['start_at']!, _startAtMeta),
      );
    } else if (isInserting) {
      context.missing(_startAtMeta);
    }
    if (data.containsKey('end_at')) {
      context.handle(
        _endAtMeta,
        endAt.isAcceptableOrUnknown(data['end_at']!, _endAtMeta),
      );
    }
    if (data.containsKey('title')) {
      context.handle(
        _titleMeta,
        title.isAcceptableOrUnknown(data['title']!, _titleMeta),
      );
    } else if (isInserting) {
      context.missing(_titleMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {referenceId, startAt};
  @override
  CachedScheduleEntry map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return CachedScheduleEntry(
      referenceId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}reference_id'],
      )!,
      type: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}type'],
      )!,
      startAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}start_at'],
      )!,
      endAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}end_at'],
      ),
      title: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}title'],
      )!,
    );
  }

  @override
  $CachedScheduleEntriesTable createAlias(String alias) {
    return $CachedScheduleEntriesTable(attachedDatabase, alias);
  }
}

class CachedScheduleEntry extends DataClass
    implements Insertable<CachedScheduleEntry> {
  final String referenceId;
  final String type;
  final DateTime startAt;
  final DateTime? endAt;
  final String title;
  const CachedScheduleEntry({
    required this.referenceId,
    required this.type,
    required this.startAt,
    this.endAt,
    required this.title,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['reference_id'] = Variable<String>(referenceId);
    map['type'] = Variable<String>(type);
    map['start_at'] = Variable<DateTime>(startAt);
    if (!nullToAbsent || endAt != null) {
      map['end_at'] = Variable<DateTime>(endAt);
    }
    map['title'] = Variable<String>(title);
    return map;
  }

  CachedScheduleEntriesCompanion toCompanion(bool nullToAbsent) {
    return CachedScheduleEntriesCompanion(
      referenceId: Value(referenceId),
      type: Value(type),
      startAt: Value(startAt),
      endAt: endAt == null && nullToAbsent
          ? const Value.absent()
          : Value(endAt),
      title: Value(title),
    );
  }

  factory CachedScheduleEntry.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return CachedScheduleEntry(
      referenceId: serializer.fromJson<String>(json['referenceId']),
      type: serializer.fromJson<String>(json['type']),
      startAt: serializer.fromJson<DateTime>(json['startAt']),
      endAt: serializer.fromJson<DateTime?>(json['endAt']),
      title: serializer.fromJson<String>(json['title']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'referenceId': serializer.toJson<String>(referenceId),
      'type': serializer.toJson<String>(type),
      'startAt': serializer.toJson<DateTime>(startAt),
      'endAt': serializer.toJson<DateTime?>(endAt),
      'title': serializer.toJson<String>(title),
    };
  }

  CachedScheduleEntry copyWith({
    String? referenceId,
    String? type,
    DateTime? startAt,
    Value<DateTime?> endAt = const Value.absent(),
    String? title,
  }) => CachedScheduleEntry(
    referenceId: referenceId ?? this.referenceId,
    type: type ?? this.type,
    startAt: startAt ?? this.startAt,
    endAt: endAt.present ? endAt.value : this.endAt,
    title: title ?? this.title,
  );
  CachedScheduleEntry copyWithCompanion(CachedScheduleEntriesCompanion data) {
    return CachedScheduleEntry(
      referenceId: data.referenceId.present
          ? data.referenceId.value
          : this.referenceId,
      type: data.type.present ? data.type.value : this.type,
      startAt: data.startAt.present ? data.startAt.value : this.startAt,
      endAt: data.endAt.present ? data.endAt.value : this.endAt,
      title: data.title.present ? data.title.value : this.title,
    );
  }

  @override
  String toString() {
    return (StringBuffer('CachedScheduleEntry(')
          ..write('referenceId: $referenceId, ')
          ..write('type: $type, ')
          ..write('startAt: $startAt, ')
          ..write('endAt: $endAt, ')
          ..write('title: $title')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(referenceId, type, startAt, endAt, title);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is CachedScheduleEntry &&
          other.referenceId == this.referenceId &&
          other.type == this.type &&
          other.startAt == this.startAt &&
          other.endAt == this.endAt &&
          other.title == this.title);
}

class CachedScheduleEntriesCompanion
    extends UpdateCompanion<CachedScheduleEntry> {
  final Value<String> referenceId;
  final Value<String> type;
  final Value<DateTime> startAt;
  final Value<DateTime?> endAt;
  final Value<String> title;
  final Value<int> rowid;
  const CachedScheduleEntriesCompanion({
    this.referenceId = const Value.absent(),
    this.type = const Value.absent(),
    this.startAt = const Value.absent(),
    this.endAt = const Value.absent(),
    this.title = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  CachedScheduleEntriesCompanion.insert({
    required String referenceId,
    required String type,
    required DateTime startAt,
    this.endAt = const Value.absent(),
    required String title,
    this.rowid = const Value.absent(),
  }) : referenceId = Value(referenceId),
       type = Value(type),
       startAt = Value(startAt),
       title = Value(title);
  static Insertable<CachedScheduleEntry> custom({
    Expression<String>? referenceId,
    Expression<String>? type,
    Expression<DateTime>? startAt,
    Expression<DateTime>? endAt,
    Expression<String>? title,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (referenceId != null) 'reference_id': referenceId,
      if (type != null) 'type': type,
      if (startAt != null) 'start_at': startAt,
      if (endAt != null) 'end_at': endAt,
      if (title != null) 'title': title,
      if (rowid != null) 'rowid': rowid,
    });
  }

  CachedScheduleEntriesCompanion copyWith({
    Value<String>? referenceId,
    Value<String>? type,
    Value<DateTime>? startAt,
    Value<DateTime?>? endAt,
    Value<String>? title,
    Value<int>? rowid,
  }) {
    return CachedScheduleEntriesCompanion(
      referenceId: referenceId ?? this.referenceId,
      type: type ?? this.type,
      startAt: startAt ?? this.startAt,
      endAt: endAt ?? this.endAt,
      title: title ?? this.title,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (referenceId.present) {
      map['reference_id'] = Variable<String>(referenceId.value);
    }
    if (type.present) {
      map['type'] = Variable<String>(type.value);
    }
    if (startAt.present) {
      map['start_at'] = Variable<DateTime>(startAt.value);
    }
    if (endAt.present) {
      map['end_at'] = Variable<DateTime>(endAt.value);
    }
    if (title.present) {
      map['title'] = Variable<String>(title.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('CachedScheduleEntriesCompanion(')
          ..write('referenceId: $referenceId, ')
          ..write('type: $type, ')
          ..write('startAt: $startAt, ')
          ..write('endAt: $endAt, ')
          ..write('title: $title, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $CachedMapAssetsTable extends CachedMapAssets
    with TableInfo<$CachedMapAssetsTable, CachedMapAsset> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $CachedMapAssetsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _assetTypeMeta = const VerificationMeta(
    'assetType',
  );
  @override
  late final GeneratedColumn<String> assetType = GeneratedColumn<String>(
    'asset_type',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _assetIdMeta = const VerificationMeta(
    'assetId',
  );
  @override
  late final GeneratedColumn<String> assetId = GeneratedColumn<String>(
    'asset_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _titleMeta = const VerificationMeta('title');
  @override
  late final GeneratedColumn<String> title = GeneratedColumn<String>(
    'title',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _subtitleMeta = const VerificationMeta(
    'subtitle',
  );
  @override
  late final GeneratedColumn<String> subtitle = GeneratedColumn<String>(
    'subtitle',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _latitudeMeta = const VerificationMeta(
    'latitude',
  );
  @override
  late final GeneratedColumn<double> latitude = GeneratedColumn<double>(
    'latitude',
    aliasedName,
    false,
    type: DriftSqlType.double,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _longitudeMeta = const VerificationMeta(
    'longitude',
  );
  @override
  late final GeneratedColumn<double> longitude = GeneratedColumn<double>(
    'longitude',
    aliasedName,
    false,
    type: DriftSqlType.double,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _statusMeta = const VerificationMeta('status');
  @override
  late final GeneratedColumn<String> status = GeneratedColumn<String>(
    'status',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _updatedAtMeta = const VerificationMeta(
    'updatedAt',
  );
  @override
  late final GeneratedColumn<DateTime> updatedAt = GeneratedColumn<DateTime>(
    'updated_at',
    aliasedName,
    true,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _cachedAtMeta = const VerificationMeta(
    'cachedAt',
  );
  @override
  late final GeneratedColumn<DateTime> cachedAt = GeneratedColumn<DateTime>(
    'cached_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [
    assetType,
    assetId,
    title,
    subtitle,
    latitude,
    longitude,
    status,
    updatedAt,
    cachedAt,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'cached_map_assets';
  @override
  VerificationContext validateIntegrity(
    Insertable<CachedMapAsset> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('asset_type')) {
      context.handle(
        _assetTypeMeta,
        assetType.isAcceptableOrUnknown(data['asset_type']!, _assetTypeMeta),
      );
    } else if (isInserting) {
      context.missing(_assetTypeMeta);
    }
    if (data.containsKey('asset_id')) {
      context.handle(
        _assetIdMeta,
        assetId.isAcceptableOrUnknown(data['asset_id']!, _assetIdMeta),
      );
    } else if (isInserting) {
      context.missing(_assetIdMeta);
    }
    if (data.containsKey('title')) {
      context.handle(
        _titleMeta,
        title.isAcceptableOrUnknown(data['title']!, _titleMeta),
      );
    } else if (isInserting) {
      context.missing(_titleMeta);
    }
    if (data.containsKey('subtitle')) {
      context.handle(
        _subtitleMeta,
        subtitle.isAcceptableOrUnknown(data['subtitle']!, _subtitleMeta),
      );
    }
    if (data.containsKey('latitude')) {
      context.handle(
        _latitudeMeta,
        latitude.isAcceptableOrUnknown(data['latitude']!, _latitudeMeta),
      );
    } else if (isInserting) {
      context.missing(_latitudeMeta);
    }
    if (data.containsKey('longitude')) {
      context.handle(
        _longitudeMeta,
        longitude.isAcceptableOrUnknown(data['longitude']!, _longitudeMeta),
      );
    } else if (isInserting) {
      context.missing(_longitudeMeta);
    }
    if (data.containsKey('status')) {
      context.handle(
        _statusMeta,
        status.isAcceptableOrUnknown(data['status']!, _statusMeta),
      );
    }
    if (data.containsKey('updated_at')) {
      context.handle(
        _updatedAtMeta,
        updatedAt.isAcceptableOrUnknown(data['updated_at']!, _updatedAtMeta),
      );
    }
    if (data.containsKey('cached_at')) {
      context.handle(
        _cachedAtMeta,
        cachedAt.isAcceptableOrUnknown(data['cached_at']!, _cachedAtMeta),
      );
    } else if (isInserting) {
      context.missing(_cachedAtMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {assetType, assetId};
  @override
  CachedMapAsset map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return CachedMapAsset(
      assetType: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}asset_type'],
      )!,
      assetId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}asset_id'],
      )!,
      title: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}title'],
      )!,
      subtitle: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}subtitle'],
      ),
      latitude: attachedDatabase.typeMapping.read(
        DriftSqlType.double,
        data['${effectivePrefix}latitude'],
      )!,
      longitude: attachedDatabase.typeMapping.read(
        DriftSqlType.double,
        data['${effectivePrefix}longitude'],
      )!,
      status: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}status'],
      ),
      updatedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}updated_at'],
      ),
      cachedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}cached_at'],
      )!,
    );
  }

  @override
  $CachedMapAssetsTable createAlias(String alias) {
    return $CachedMapAssetsTable(attachedDatabase, alias);
  }
}

class CachedMapAsset extends DataClass implements Insertable<CachedMapAsset> {
  final String assetType;
  final String assetId;
  final String title;
  final String? subtitle;
  final double latitude;
  final double longitude;
  final String? status;
  final DateTime? updatedAt;
  final DateTime cachedAt;
  const CachedMapAsset({
    required this.assetType,
    required this.assetId,
    required this.title,
    this.subtitle,
    required this.latitude,
    required this.longitude,
    this.status,
    this.updatedAt,
    required this.cachedAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['asset_type'] = Variable<String>(assetType);
    map['asset_id'] = Variable<String>(assetId);
    map['title'] = Variable<String>(title);
    if (!nullToAbsent || subtitle != null) {
      map['subtitle'] = Variable<String>(subtitle);
    }
    map['latitude'] = Variable<double>(latitude);
    map['longitude'] = Variable<double>(longitude);
    if (!nullToAbsent || status != null) {
      map['status'] = Variable<String>(status);
    }
    if (!nullToAbsent || updatedAt != null) {
      map['updated_at'] = Variable<DateTime>(updatedAt);
    }
    map['cached_at'] = Variable<DateTime>(cachedAt);
    return map;
  }

  CachedMapAssetsCompanion toCompanion(bool nullToAbsent) {
    return CachedMapAssetsCompanion(
      assetType: Value(assetType),
      assetId: Value(assetId),
      title: Value(title),
      subtitle: subtitle == null && nullToAbsent
          ? const Value.absent()
          : Value(subtitle),
      latitude: Value(latitude),
      longitude: Value(longitude),
      status: status == null && nullToAbsent
          ? const Value.absent()
          : Value(status),
      updatedAt: updatedAt == null && nullToAbsent
          ? const Value.absent()
          : Value(updatedAt),
      cachedAt: Value(cachedAt),
    );
  }

  factory CachedMapAsset.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return CachedMapAsset(
      assetType: serializer.fromJson<String>(json['assetType']),
      assetId: serializer.fromJson<String>(json['assetId']),
      title: serializer.fromJson<String>(json['title']),
      subtitle: serializer.fromJson<String?>(json['subtitle']),
      latitude: serializer.fromJson<double>(json['latitude']),
      longitude: serializer.fromJson<double>(json['longitude']),
      status: serializer.fromJson<String?>(json['status']),
      updatedAt: serializer.fromJson<DateTime?>(json['updatedAt']),
      cachedAt: serializer.fromJson<DateTime>(json['cachedAt']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'assetType': serializer.toJson<String>(assetType),
      'assetId': serializer.toJson<String>(assetId),
      'title': serializer.toJson<String>(title),
      'subtitle': serializer.toJson<String?>(subtitle),
      'latitude': serializer.toJson<double>(latitude),
      'longitude': serializer.toJson<double>(longitude),
      'status': serializer.toJson<String?>(status),
      'updatedAt': serializer.toJson<DateTime?>(updatedAt),
      'cachedAt': serializer.toJson<DateTime>(cachedAt),
    };
  }

  CachedMapAsset copyWith({
    String? assetType,
    String? assetId,
    String? title,
    Value<String?> subtitle = const Value.absent(),
    double? latitude,
    double? longitude,
    Value<String?> status = const Value.absent(),
    Value<DateTime?> updatedAt = const Value.absent(),
    DateTime? cachedAt,
  }) => CachedMapAsset(
    assetType: assetType ?? this.assetType,
    assetId: assetId ?? this.assetId,
    title: title ?? this.title,
    subtitle: subtitle.present ? subtitle.value : this.subtitle,
    latitude: latitude ?? this.latitude,
    longitude: longitude ?? this.longitude,
    status: status.present ? status.value : this.status,
    updatedAt: updatedAt.present ? updatedAt.value : this.updatedAt,
    cachedAt: cachedAt ?? this.cachedAt,
  );
  CachedMapAsset copyWithCompanion(CachedMapAssetsCompanion data) {
    return CachedMapAsset(
      assetType: data.assetType.present ? data.assetType.value : this.assetType,
      assetId: data.assetId.present ? data.assetId.value : this.assetId,
      title: data.title.present ? data.title.value : this.title,
      subtitle: data.subtitle.present ? data.subtitle.value : this.subtitle,
      latitude: data.latitude.present ? data.latitude.value : this.latitude,
      longitude: data.longitude.present ? data.longitude.value : this.longitude,
      status: data.status.present ? data.status.value : this.status,
      updatedAt: data.updatedAt.present ? data.updatedAt.value : this.updatedAt,
      cachedAt: data.cachedAt.present ? data.cachedAt.value : this.cachedAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('CachedMapAsset(')
          ..write('assetType: $assetType, ')
          ..write('assetId: $assetId, ')
          ..write('title: $title, ')
          ..write('subtitle: $subtitle, ')
          ..write('latitude: $latitude, ')
          ..write('longitude: $longitude, ')
          ..write('status: $status, ')
          ..write('updatedAt: $updatedAt, ')
          ..write('cachedAt: $cachedAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    assetType,
    assetId,
    title,
    subtitle,
    latitude,
    longitude,
    status,
    updatedAt,
    cachedAt,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is CachedMapAsset &&
          other.assetType == this.assetType &&
          other.assetId == this.assetId &&
          other.title == this.title &&
          other.subtitle == this.subtitle &&
          other.latitude == this.latitude &&
          other.longitude == this.longitude &&
          other.status == this.status &&
          other.updatedAt == this.updatedAt &&
          other.cachedAt == this.cachedAt);
}

class CachedMapAssetsCompanion extends UpdateCompanion<CachedMapAsset> {
  final Value<String> assetType;
  final Value<String> assetId;
  final Value<String> title;
  final Value<String?> subtitle;
  final Value<double> latitude;
  final Value<double> longitude;
  final Value<String?> status;
  final Value<DateTime?> updatedAt;
  final Value<DateTime> cachedAt;
  final Value<int> rowid;
  const CachedMapAssetsCompanion({
    this.assetType = const Value.absent(),
    this.assetId = const Value.absent(),
    this.title = const Value.absent(),
    this.subtitle = const Value.absent(),
    this.latitude = const Value.absent(),
    this.longitude = const Value.absent(),
    this.status = const Value.absent(),
    this.updatedAt = const Value.absent(),
    this.cachedAt = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  CachedMapAssetsCompanion.insert({
    required String assetType,
    required String assetId,
    required String title,
    this.subtitle = const Value.absent(),
    required double latitude,
    required double longitude,
    this.status = const Value.absent(),
    this.updatedAt = const Value.absent(),
    required DateTime cachedAt,
    this.rowid = const Value.absent(),
  }) : assetType = Value(assetType),
       assetId = Value(assetId),
       title = Value(title),
       latitude = Value(latitude),
       longitude = Value(longitude),
       cachedAt = Value(cachedAt);
  static Insertable<CachedMapAsset> custom({
    Expression<String>? assetType,
    Expression<String>? assetId,
    Expression<String>? title,
    Expression<String>? subtitle,
    Expression<double>? latitude,
    Expression<double>? longitude,
    Expression<String>? status,
    Expression<DateTime>? updatedAt,
    Expression<DateTime>? cachedAt,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (assetType != null) 'asset_type': assetType,
      if (assetId != null) 'asset_id': assetId,
      if (title != null) 'title': title,
      if (subtitle != null) 'subtitle': subtitle,
      if (latitude != null) 'latitude': latitude,
      if (longitude != null) 'longitude': longitude,
      if (status != null) 'status': status,
      if (updatedAt != null) 'updated_at': updatedAt,
      if (cachedAt != null) 'cached_at': cachedAt,
      if (rowid != null) 'rowid': rowid,
    });
  }

  CachedMapAssetsCompanion copyWith({
    Value<String>? assetType,
    Value<String>? assetId,
    Value<String>? title,
    Value<String?>? subtitle,
    Value<double>? latitude,
    Value<double>? longitude,
    Value<String?>? status,
    Value<DateTime?>? updatedAt,
    Value<DateTime>? cachedAt,
    Value<int>? rowid,
  }) {
    return CachedMapAssetsCompanion(
      assetType: assetType ?? this.assetType,
      assetId: assetId ?? this.assetId,
      title: title ?? this.title,
      subtitle: subtitle ?? this.subtitle,
      latitude: latitude ?? this.latitude,
      longitude: longitude ?? this.longitude,
      status: status ?? this.status,
      updatedAt: updatedAt ?? this.updatedAt,
      cachedAt: cachedAt ?? this.cachedAt,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (assetType.present) {
      map['asset_type'] = Variable<String>(assetType.value);
    }
    if (assetId.present) {
      map['asset_id'] = Variable<String>(assetId.value);
    }
    if (title.present) {
      map['title'] = Variable<String>(title.value);
    }
    if (subtitle.present) {
      map['subtitle'] = Variable<String>(subtitle.value);
    }
    if (latitude.present) {
      map['latitude'] = Variable<double>(latitude.value);
    }
    if (longitude.present) {
      map['longitude'] = Variable<double>(longitude.value);
    }
    if (status.present) {
      map['status'] = Variable<String>(status.value);
    }
    if (updatedAt.present) {
      map['updated_at'] = Variable<DateTime>(updatedAt.value);
    }
    if (cachedAt.present) {
      map['cached_at'] = Variable<DateTime>(cachedAt.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('CachedMapAssetsCompanion(')
          ..write('assetType: $assetType, ')
          ..write('assetId: $assetId, ')
          ..write('title: $title, ')
          ..write('subtitle: $subtitle, ')
          ..write('latitude: $latitude, ')
          ..write('longitude: $longitude, ')
          ..write('status: $status, ')
          ..write('updatedAt: $updatedAt, ')
          ..write('cachedAt: $cachedAt, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $CachedMapAssetSyncCursorsTable extends CachedMapAssetSyncCursors
    with TableInfo<$CachedMapAssetSyncCursorsTable, CachedMapAssetSyncCursor> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $CachedMapAssetSyncCursorsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _assetTypeMeta = const VerificationMeta(
    'assetType',
  );
  @override
  late final GeneratedColumn<String> assetType = GeneratedColumn<String>(
    'asset_type',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _syncedAtMeta = const VerificationMeta(
    'syncedAt',
  );
  @override
  late final GeneratedColumn<DateTime> syncedAt = GeneratedColumn<DateTime>(
    'synced_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [assetType, syncedAt];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'cached_map_asset_sync_cursors';
  @override
  VerificationContext validateIntegrity(
    Insertable<CachedMapAssetSyncCursor> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('asset_type')) {
      context.handle(
        _assetTypeMeta,
        assetType.isAcceptableOrUnknown(data['asset_type']!, _assetTypeMeta),
      );
    } else if (isInserting) {
      context.missing(_assetTypeMeta);
    }
    if (data.containsKey('synced_at')) {
      context.handle(
        _syncedAtMeta,
        syncedAt.isAcceptableOrUnknown(data['synced_at']!, _syncedAtMeta),
      );
    } else if (isInserting) {
      context.missing(_syncedAtMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {assetType};
  @override
  CachedMapAssetSyncCursor map(
    Map<String, dynamic> data, {
    String? tablePrefix,
  }) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return CachedMapAssetSyncCursor(
      assetType: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}asset_type'],
      )!,
      syncedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}synced_at'],
      )!,
    );
  }

  @override
  $CachedMapAssetSyncCursorsTable createAlias(String alias) {
    return $CachedMapAssetSyncCursorsTable(attachedDatabase, alias);
  }
}

class CachedMapAssetSyncCursor extends DataClass
    implements Insertable<CachedMapAssetSyncCursor> {
  final String assetType;
  final DateTime syncedAt;
  const CachedMapAssetSyncCursor({
    required this.assetType,
    required this.syncedAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['asset_type'] = Variable<String>(assetType);
    map['synced_at'] = Variable<DateTime>(syncedAt);
    return map;
  }

  CachedMapAssetSyncCursorsCompanion toCompanion(bool nullToAbsent) {
    return CachedMapAssetSyncCursorsCompanion(
      assetType: Value(assetType),
      syncedAt: Value(syncedAt),
    );
  }

  factory CachedMapAssetSyncCursor.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return CachedMapAssetSyncCursor(
      assetType: serializer.fromJson<String>(json['assetType']),
      syncedAt: serializer.fromJson<DateTime>(json['syncedAt']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'assetType': serializer.toJson<String>(assetType),
      'syncedAt': serializer.toJson<DateTime>(syncedAt),
    };
  }

  CachedMapAssetSyncCursor copyWith({String? assetType, DateTime? syncedAt}) =>
      CachedMapAssetSyncCursor(
        assetType: assetType ?? this.assetType,
        syncedAt: syncedAt ?? this.syncedAt,
      );
  CachedMapAssetSyncCursor copyWithCompanion(
    CachedMapAssetSyncCursorsCompanion data,
  ) {
    return CachedMapAssetSyncCursor(
      assetType: data.assetType.present ? data.assetType.value : this.assetType,
      syncedAt: data.syncedAt.present ? data.syncedAt.value : this.syncedAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('CachedMapAssetSyncCursor(')
          ..write('assetType: $assetType, ')
          ..write('syncedAt: $syncedAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(assetType, syncedAt);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is CachedMapAssetSyncCursor &&
          other.assetType == this.assetType &&
          other.syncedAt == this.syncedAt);
}

class CachedMapAssetSyncCursorsCompanion
    extends UpdateCompanion<CachedMapAssetSyncCursor> {
  final Value<String> assetType;
  final Value<DateTime> syncedAt;
  final Value<int> rowid;
  const CachedMapAssetSyncCursorsCompanion({
    this.assetType = const Value.absent(),
    this.syncedAt = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  CachedMapAssetSyncCursorsCompanion.insert({
    required String assetType,
    required DateTime syncedAt,
    this.rowid = const Value.absent(),
  }) : assetType = Value(assetType),
       syncedAt = Value(syncedAt);
  static Insertable<CachedMapAssetSyncCursor> custom({
    Expression<String>? assetType,
    Expression<DateTime>? syncedAt,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (assetType != null) 'asset_type': assetType,
      if (syncedAt != null) 'synced_at': syncedAt,
      if (rowid != null) 'rowid': rowid,
    });
  }

  CachedMapAssetSyncCursorsCompanion copyWith({
    Value<String>? assetType,
    Value<DateTime>? syncedAt,
    Value<int>? rowid,
  }) {
    return CachedMapAssetSyncCursorsCompanion(
      assetType: assetType ?? this.assetType,
      syncedAt: syncedAt ?? this.syncedAt,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (assetType.present) {
      map['asset_type'] = Variable<String>(assetType.value);
    }
    if (syncedAt.present) {
      map['synced_at'] = Variable<DateTime>(syncedAt.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('CachedMapAssetSyncCursorsCompanion(')
          ..write('assetType: $assetType, ')
          ..write('syncedAt: $syncedAt, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $OutboxEntriesTable extends OutboxEntries
    with TableInfo<$OutboxEntriesTable, OutboxEntry> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $OutboxEntriesTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _seqMeta = const VerificationMeta('seq');
  @override
  late final GeneratedColumn<int> seq = GeneratedColumn<int>(
    'seq',
    aliasedName,
    false,
    hasAutoIncrement: true,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'PRIMARY KEY AUTOINCREMENT',
    ),
  );
  static const VerificationMeta _clientRefMeta = const VerificationMeta(
    'clientRef',
  );
  @override
  late final GeneratedColumn<String> clientRef = GeneratedColumn<String>(
    'client_ref',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
    defaultConstraints: GeneratedColumn.constraintIsAlways('UNIQUE'),
  );
  static const VerificationMeta _kindMeta = const VerificationMeta('kind');
  @override
  late final GeneratedColumn<String> kind = GeneratedColumn<String>(
    'kind',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _payloadJsonMeta = const VerificationMeta(
    'payloadJson',
  );
  @override
  late final GeneratedColumn<String> payloadJson = GeneratedColumn<String>(
    'payload_json',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _statusMeta = const VerificationMeta('status');
  @override
  late final GeneratedColumn<String> status = GeneratedColumn<String>(
    'status',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
    defaultValue: const Constant('pending'),
  );
  static const VerificationMeta _attemptsMeta = const VerificationMeta(
    'attempts',
  );
  @override
  late final GeneratedColumn<int> attempts = GeneratedColumn<int>(
    'attempts',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(0),
  );
  static const VerificationMeta _lastErrorMeta = const VerificationMeta(
    'lastError',
  );
  @override
  late final GeneratedColumn<String> lastError = GeneratedColumn<String>(
    'last_error',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _createdAtMeta = const VerificationMeta(
    'createdAt',
  );
  @override
  late final GeneratedColumn<DateTime> createdAt = GeneratedColumn<DateTime>(
    'created_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [
    seq,
    clientRef,
    kind,
    payloadJson,
    status,
    attempts,
    lastError,
    createdAt,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'outbox_entries';
  @override
  VerificationContext validateIntegrity(
    Insertable<OutboxEntry> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('seq')) {
      context.handle(
        _seqMeta,
        seq.isAcceptableOrUnknown(data['seq']!, _seqMeta),
      );
    }
    if (data.containsKey('client_ref')) {
      context.handle(
        _clientRefMeta,
        clientRef.isAcceptableOrUnknown(data['client_ref']!, _clientRefMeta),
      );
    } else if (isInserting) {
      context.missing(_clientRefMeta);
    }
    if (data.containsKey('kind')) {
      context.handle(
        _kindMeta,
        kind.isAcceptableOrUnknown(data['kind']!, _kindMeta),
      );
    } else if (isInserting) {
      context.missing(_kindMeta);
    }
    if (data.containsKey('payload_json')) {
      context.handle(
        _payloadJsonMeta,
        payloadJson.isAcceptableOrUnknown(
          data['payload_json']!,
          _payloadJsonMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_payloadJsonMeta);
    }
    if (data.containsKey('status')) {
      context.handle(
        _statusMeta,
        status.isAcceptableOrUnknown(data['status']!, _statusMeta),
      );
    }
    if (data.containsKey('attempts')) {
      context.handle(
        _attemptsMeta,
        attempts.isAcceptableOrUnknown(data['attempts']!, _attemptsMeta),
      );
    }
    if (data.containsKey('last_error')) {
      context.handle(
        _lastErrorMeta,
        lastError.isAcceptableOrUnknown(data['last_error']!, _lastErrorMeta),
      );
    }
    if (data.containsKey('created_at')) {
      context.handle(
        _createdAtMeta,
        createdAt.isAcceptableOrUnknown(data['created_at']!, _createdAtMeta),
      );
    } else if (isInserting) {
      context.missing(_createdAtMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {seq};
  @override
  OutboxEntry map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return OutboxEntry(
      seq: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}seq'],
      )!,
      clientRef: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}client_ref'],
      )!,
      kind: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}kind'],
      )!,
      payloadJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}payload_json'],
      )!,
      status: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}status'],
      )!,
      attempts: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}attempts'],
      )!,
      lastError: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}last_error'],
      ),
      createdAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}created_at'],
      )!,
    );
  }

  @override
  $OutboxEntriesTable createAlias(String alias) {
    return $OutboxEntriesTable(attachedDatabase, alias);
  }
}

class OutboxEntry extends DataClass implements Insertable<OutboxEntry> {
  final int seq;
  final String clientRef;
  final String kind;
  final String payloadJson;
  final String status;
  final int attempts;
  final String? lastError;
  final DateTime createdAt;
  const OutboxEntry({
    required this.seq,
    required this.clientRef,
    required this.kind,
    required this.payloadJson,
    required this.status,
    required this.attempts,
    this.lastError,
    required this.createdAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['seq'] = Variable<int>(seq);
    map['client_ref'] = Variable<String>(clientRef);
    map['kind'] = Variable<String>(kind);
    map['payload_json'] = Variable<String>(payloadJson);
    map['status'] = Variable<String>(status);
    map['attempts'] = Variable<int>(attempts);
    if (!nullToAbsent || lastError != null) {
      map['last_error'] = Variable<String>(lastError);
    }
    map['created_at'] = Variable<DateTime>(createdAt);
    return map;
  }

  OutboxEntriesCompanion toCompanion(bool nullToAbsent) {
    return OutboxEntriesCompanion(
      seq: Value(seq),
      clientRef: Value(clientRef),
      kind: Value(kind),
      payloadJson: Value(payloadJson),
      status: Value(status),
      attempts: Value(attempts),
      lastError: lastError == null && nullToAbsent
          ? const Value.absent()
          : Value(lastError),
      createdAt: Value(createdAt),
    );
  }

  factory OutboxEntry.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return OutboxEntry(
      seq: serializer.fromJson<int>(json['seq']),
      clientRef: serializer.fromJson<String>(json['clientRef']),
      kind: serializer.fromJson<String>(json['kind']),
      payloadJson: serializer.fromJson<String>(json['payloadJson']),
      status: serializer.fromJson<String>(json['status']),
      attempts: serializer.fromJson<int>(json['attempts']),
      lastError: serializer.fromJson<String?>(json['lastError']),
      createdAt: serializer.fromJson<DateTime>(json['createdAt']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'seq': serializer.toJson<int>(seq),
      'clientRef': serializer.toJson<String>(clientRef),
      'kind': serializer.toJson<String>(kind),
      'payloadJson': serializer.toJson<String>(payloadJson),
      'status': serializer.toJson<String>(status),
      'attempts': serializer.toJson<int>(attempts),
      'lastError': serializer.toJson<String?>(lastError),
      'createdAt': serializer.toJson<DateTime>(createdAt),
    };
  }

  OutboxEntry copyWith({
    int? seq,
    String? clientRef,
    String? kind,
    String? payloadJson,
    String? status,
    int? attempts,
    Value<String?> lastError = const Value.absent(),
    DateTime? createdAt,
  }) => OutboxEntry(
    seq: seq ?? this.seq,
    clientRef: clientRef ?? this.clientRef,
    kind: kind ?? this.kind,
    payloadJson: payloadJson ?? this.payloadJson,
    status: status ?? this.status,
    attempts: attempts ?? this.attempts,
    lastError: lastError.present ? lastError.value : this.lastError,
    createdAt: createdAt ?? this.createdAt,
  );
  OutboxEntry copyWithCompanion(OutboxEntriesCompanion data) {
    return OutboxEntry(
      seq: data.seq.present ? data.seq.value : this.seq,
      clientRef: data.clientRef.present ? data.clientRef.value : this.clientRef,
      kind: data.kind.present ? data.kind.value : this.kind,
      payloadJson: data.payloadJson.present
          ? data.payloadJson.value
          : this.payloadJson,
      status: data.status.present ? data.status.value : this.status,
      attempts: data.attempts.present ? data.attempts.value : this.attempts,
      lastError: data.lastError.present ? data.lastError.value : this.lastError,
      createdAt: data.createdAt.present ? data.createdAt.value : this.createdAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('OutboxEntry(')
          ..write('seq: $seq, ')
          ..write('clientRef: $clientRef, ')
          ..write('kind: $kind, ')
          ..write('payloadJson: $payloadJson, ')
          ..write('status: $status, ')
          ..write('attempts: $attempts, ')
          ..write('lastError: $lastError, ')
          ..write('createdAt: $createdAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    seq,
    clientRef,
    kind,
    payloadJson,
    status,
    attempts,
    lastError,
    createdAt,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is OutboxEntry &&
          other.seq == this.seq &&
          other.clientRef == this.clientRef &&
          other.kind == this.kind &&
          other.payloadJson == this.payloadJson &&
          other.status == this.status &&
          other.attempts == this.attempts &&
          other.lastError == this.lastError &&
          other.createdAt == this.createdAt);
}

class OutboxEntriesCompanion extends UpdateCompanion<OutboxEntry> {
  final Value<int> seq;
  final Value<String> clientRef;
  final Value<String> kind;
  final Value<String> payloadJson;
  final Value<String> status;
  final Value<int> attempts;
  final Value<String?> lastError;
  final Value<DateTime> createdAt;
  const OutboxEntriesCompanion({
    this.seq = const Value.absent(),
    this.clientRef = const Value.absent(),
    this.kind = const Value.absent(),
    this.payloadJson = const Value.absent(),
    this.status = const Value.absent(),
    this.attempts = const Value.absent(),
    this.lastError = const Value.absent(),
    this.createdAt = const Value.absent(),
  });
  OutboxEntriesCompanion.insert({
    this.seq = const Value.absent(),
    required String clientRef,
    required String kind,
    required String payloadJson,
    this.status = const Value.absent(),
    this.attempts = const Value.absent(),
    this.lastError = const Value.absent(),
    required DateTime createdAt,
  }) : clientRef = Value(clientRef),
       kind = Value(kind),
       payloadJson = Value(payloadJson),
       createdAt = Value(createdAt);
  static Insertable<OutboxEntry> custom({
    Expression<int>? seq,
    Expression<String>? clientRef,
    Expression<String>? kind,
    Expression<String>? payloadJson,
    Expression<String>? status,
    Expression<int>? attempts,
    Expression<String>? lastError,
    Expression<DateTime>? createdAt,
  }) {
    return RawValuesInsertable({
      if (seq != null) 'seq': seq,
      if (clientRef != null) 'client_ref': clientRef,
      if (kind != null) 'kind': kind,
      if (payloadJson != null) 'payload_json': payloadJson,
      if (status != null) 'status': status,
      if (attempts != null) 'attempts': attempts,
      if (lastError != null) 'last_error': lastError,
      if (createdAt != null) 'created_at': createdAt,
    });
  }

  OutboxEntriesCompanion copyWith({
    Value<int>? seq,
    Value<String>? clientRef,
    Value<String>? kind,
    Value<String>? payloadJson,
    Value<String>? status,
    Value<int>? attempts,
    Value<String?>? lastError,
    Value<DateTime>? createdAt,
  }) {
    return OutboxEntriesCompanion(
      seq: seq ?? this.seq,
      clientRef: clientRef ?? this.clientRef,
      kind: kind ?? this.kind,
      payloadJson: payloadJson ?? this.payloadJson,
      status: status ?? this.status,
      attempts: attempts ?? this.attempts,
      lastError: lastError ?? this.lastError,
      createdAt: createdAt ?? this.createdAt,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (seq.present) {
      map['seq'] = Variable<int>(seq.value);
    }
    if (clientRef.present) {
      map['client_ref'] = Variable<String>(clientRef.value);
    }
    if (kind.present) {
      map['kind'] = Variable<String>(kind.value);
    }
    if (payloadJson.present) {
      map['payload_json'] = Variable<String>(payloadJson.value);
    }
    if (status.present) {
      map['status'] = Variable<String>(status.value);
    }
    if (attempts.present) {
      map['attempts'] = Variable<int>(attempts.value);
    }
    if (lastError.present) {
      map['last_error'] = Variable<String>(lastError.value);
    }
    if (createdAt.present) {
      map['created_at'] = Variable<DateTime>(createdAt.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('OutboxEntriesCompanion(')
          ..write('seq: $seq, ')
          ..write('clientRef: $clientRef, ')
          ..write('kind: $kind, ')
          ..write('payloadJson: $payloadJson, ')
          ..write('status: $status, ')
          ..write('attempts: $attempts, ')
          ..write('lastError: $lastError, ')
          ..write('createdAt: $createdAt')
          ..write(')'))
        .toString();
  }
}

class $PendingPhotosTable extends PendingPhotos
    with TableInfo<$PendingPhotosTable, PendingPhoto> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $PendingPhotosTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _clientRefMeta = const VerificationMeta(
    'clientRef',
  );
  @override
  late final GeneratedColumn<String> clientRef = GeneratedColumn<String>(
    'client_ref',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _localPathMeta = const VerificationMeta(
    'localPath',
  );
  @override
  late final GeneratedColumn<String> localPath = GeneratedColumn<String>(
    'local_path',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _kindMeta = const VerificationMeta('kind');
  @override
  late final GeneratedColumn<String> kind = GeneratedColumn<String>(
    'kind',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
    defaultValue: const Constant('photo'),
  );
  static const VerificationMeta _workOrderIdMeta = const VerificationMeta(
    'workOrderId',
  );
  @override
  late final GeneratedColumn<String> workOrderId = GeneratedColumn<String>(
    'work_order_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _installationProjectIdMeta =
      const VerificationMeta('installationProjectId');
  @override
  late final GeneratedColumn<String> installationProjectId =
      GeneratedColumn<String>(
        'installation_project_id',
        aliasedName,
        true,
        type: DriftSqlType.string,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _latitudeMeta = const VerificationMeta(
    'latitude',
  );
  @override
  late final GeneratedColumn<double> latitude = GeneratedColumn<double>(
    'latitude',
    aliasedName,
    true,
    type: DriftSqlType.double,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _longitudeMeta = const VerificationMeta(
    'longitude',
  );
  @override
  late final GeneratedColumn<double> longitude = GeneratedColumn<double>(
    'longitude',
    aliasedName,
    true,
    type: DriftSqlType.double,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _capturedAtMeta = const VerificationMeta(
    'capturedAt',
  );
  @override
  late final GeneratedColumn<DateTime> capturedAt = GeneratedColumn<DateTime>(
    'captured_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _uploadedMeta = const VerificationMeta(
    'uploaded',
  );
  @override
  late final GeneratedColumn<bool> uploaded = GeneratedColumn<bool>(
    'uploaded',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("uploaded" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _failedMeta = const VerificationMeta('failed');
  @override
  late final GeneratedColumn<bool> failed = GeneratedColumn<bool>(
    'failed',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("failed" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _lastErrorMeta = const VerificationMeta(
    'lastError',
  );
  @override
  late final GeneratedColumn<String> lastError = GeneratedColumn<String>(
    'last_error',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  @override
  List<GeneratedColumn> get $columns => [
    clientRef,
    localPath,
    kind,
    workOrderId,
    installationProjectId,
    latitude,
    longitude,
    capturedAt,
    uploaded,
    failed,
    lastError,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'pending_photos';
  @override
  VerificationContext validateIntegrity(
    Insertable<PendingPhoto> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('client_ref')) {
      context.handle(
        _clientRefMeta,
        clientRef.isAcceptableOrUnknown(data['client_ref']!, _clientRefMeta),
      );
    } else if (isInserting) {
      context.missing(_clientRefMeta);
    }
    if (data.containsKey('local_path')) {
      context.handle(
        _localPathMeta,
        localPath.isAcceptableOrUnknown(data['local_path']!, _localPathMeta),
      );
    } else if (isInserting) {
      context.missing(_localPathMeta);
    }
    if (data.containsKey('kind')) {
      context.handle(
        _kindMeta,
        kind.isAcceptableOrUnknown(data['kind']!, _kindMeta),
      );
    }
    if (data.containsKey('work_order_id')) {
      context.handle(
        _workOrderIdMeta,
        workOrderId.isAcceptableOrUnknown(
          data['work_order_id']!,
          _workOrderIdMeta,
        ),
      );
    }
    if (data.containsKey('installation_project_id')) {
      context.handle(
        _installationProjectIdMeta,
        installationProjectId.isAcceptableOrUnknown(
          data['installation_project_id']!,
          _installationProjectIdMeta,
        ),
      );
    }
    if (data.containsKey('latitude')) {
      context.handle(
        _latitudeMeta,
        latitude.isAcceptableOrUnknown(data['latitude']!, _latitudeMeta),
      );
    }
    if (data.containsKey('longitude')) {
      context.handle(
        _longitudeMeta,
        longitude.isAcceptableOrUnknown(data['longitude']!, _longitudeMeta),
      );
    }
    if (data.containsKey('captured_at')) {
      context.handle(
        _capturedAtMeta,
        capturedAt.isAcceptableOrUnknown(data['captured_at']!, _capturedAtMeta),
      );
    } else if (isInserting) {
      context.missing(_capturedAtMeta);
    }
    if (data.containsKey('uploaded')) {
      context.handle(
        _uploadedMeta,
        uploaded.isAcceptableOrUnknown(data['uploaded']!, _uploadedMeta),
      );
    }
    if (data.containsKey('failed')) {
      context.handle(
        _failedMeta,
        failed.isAcceptableOrUnknown(data['failed']!, _failedMeta),
      );
    }
    if (data.containsKey('last_error')) {
      context.handle(
        _lastErrorMeta,
        lastError.isAcceptableOrUnknown(data['last_error']!, _lastErrorMeta),
      );
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {clientRef};
  @override
  PendingPhoto map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return PendingPhoto(
      clientRef: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}client_ref'],
      )!,
      localPath: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}local_path'],
      )!,
      kind: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}kind'],
      )!,
      workOrderId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}work_order_id'],
      ),
      installationProjectId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}installation_project_id'],
      ),
      latitude: attachedDatabase.typeMapping.read(
        DriftSqlType.double,
        data['${effectivePrefix}latitude'],
      ),
      longitude: attachedDatabase.typeMapping.read(
        DriftSqlType.double,
        data['${effectivePrefix}longitude'],
      ),
      capturedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}captured_at'],
      )!,
      uploaded: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}uploaded'],
      )!,
      failed: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}failed'],
      )!,
      lastError: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}last_error'],
      ),
    );
  }

  @override
  $PendingPhotosTable createAlias(String alias) {
    return $PendingPhotosTable(attachedDatabase, alias);
  }
}

class PendingPhoto extends DataClass implements Insertable<PendingPhoto> {
  final String clientRef;
  final String localPath;
  final String kind;
  final String? workOrderId;
  final String? installationProjectId;
  final double? latitude;
  final double? longitude;
  final DateTime capturedAt;
  final bool uploaded;
  final bool failed;
  final String? lastError;
  const PendingPhoto({
    required this.clientRef,
    required this.localPath,
    required this.kind,
    this.workOrderId,
    this.installationProjectId,
    this.latitude,
    this.longitude,
    required this.capturedAt,
    required this.uploaded,
    required this.failed,
    this.lastError,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['client_ref'] = Variable<String>(clientRef);
    map['local_path'] = Variable<String>(localPath);
    map['kind'] = Variable<String>(kind);
    if (!nullToAbsent || workOrderId != null) {
      map['work_order_id'] = Variable<String>(workOrderId);
    }
    if (!nullToAbsent || installationProjectId != null) {
      map['installation_project_id'] = Variable<String>(installationProjectId);
    }
    if (!nullToAbsent || latitude != null) {
      map['latitude'] = Variable<double>(latitude);
    }
    if (!nullToAbsent || longitude != null) {
      map['longitude'] = Variable<double>(longitude);
    }
    map['captured_at'] = Variable<DateTime>(capturedAt);
    map['uploaded'] = Variable<bool>(uploaded);
    map['failed'] = Variable<bool>(failed);
    if (!nullToAbsent || lastError != null) {
      map['last_error'] = Variable<String>(lastError);
    }
    return map;
  }

  PendingPhotosCompanion toCompanion(bool nullToAbsent) {
    return PendingPhotosCompanion(
      clientRef: Value(clientRef),
      localPath: Value(localPath),
      kind: Value(kind),
      workOrderId: workOrderId == null && nullToAbsent
          ? const Value.absent()
          : Value(workOrderId),
      installationProjectId: installationProjectId == null && nullToAbsent
          ? const Value.absent()
          : Value(installationProjectId),
      latitude: latitude == null && nullToAbsent
          ? const Value.absent()
          : Value(latitude),
      longitude: longitude == null && nullToAbsent
          ? const Value.absent()
          : Value(longitude),
      capturedAt: Value(capturedAt),
      uploaded: Value(uploaded),
      failed: Value(failed),
      lastError: lastError == null && nullToAbsent
          ? const Value.absent()
          : Value(lastError),
    );
  }

  factory PendingPhoto.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return PendingPhoto(
      clientRef: serializer.fromJson<String>(json['clientRef']),
      localPath: serializer.fromJson<String>(json['localPath']),
      kind: serializer.fromJson<String>(json['kind']),
      workOrderId: serializer.fromJson<String?>(json['workOrderId']),
      installationProjectId: serializer.fromJson<String?>(
        json['installationProjectId'],
      ),
      latitude: serializer.fromJson<double?>(json['latitude']),
      longitude: serializer.fromJson<double?>(json['longitude']),
      capturedAt: serializer.fromJson<DateTime>(json['capturedAt']),
      uploaded: serializer.fromJson<bool>(json['uploaded']),
      failed: serializer.fromJson<bool>(json['failed']),
      lastError: serializer.fromJson<String?>(json['lastError']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'clientRef': serializer.toJson<String>(clientRef),
      'localPath': serializer.toJson<String>(localPath),
      'kind': serializer.toJson<String>(kind),
      'workOrderId': serializer.toJson<String?>(workOrderId),
      'installationProjectId': serializer.toJson<String?>(
        installationProjectId,
      ),
      'latitude': serializer.toJson<double?>(latitude),
      'longitude': serializer.toJson<double?>(longitude),
      'capturedAt': serializer.toJson<DateTime>(capturedAt),
      'uploaded': serializer.toJson<bool>(uploaded),
      'failed': serializer.toJson<bool>(failed),
      'lastError': serializer.toJson<String?>(lastError),
    };
  }

  PendingPhoto copyWith({
    String? clientRef,
    String? localPath,
    String? kind,
    Value<String?> workOrderId = const Value.absent(),
    Value<String?> installationProjectId = const Value.absent(),
    Value<double?> latitude = const Value.absent(),
    Value<double?> longitude = const Value.absent(),
    DateTime? capturedAt,
    bool? uploaded,
    bool? failed,
    Value<String?> lastError = const Value.absent(),
  }) => PendingPhoto(
    clientRef: clientRef ?? this.clientRef,
    localPath: localPath ?? this.localPath,
    kind: kind ?? this.kind,
    workOrderId: workOrderId.present ? workOrderId.value : this.workOrderId,
    installationProjectId: installationProjectId.present
        ? installationProjectId.value
        : this.installationProjectId,
    latitude: latitude.present ? latitude.value : this.latitude,
    longitude: longitude.present ? longitude.value : this.longitude,
    capturedAt: capturedAt ?? this.capturedAt,
    uploaded: uploaded ?? this.uploaded,
    failed: failed ?? this.failed,
    lastError: lastError.present ? lastError.value : this.lastError,
  );
  PendingPhoto copyWithCompanion(PendingPhotosCompanion data) {
    return PendingPhoto(
      clientRef: data.clientRef.present ? data.clientRef.value : this.clientRef,
      localPath: data.localPath.present ? data.localPath.value : this.localPath,
      kind: data.kind.present ? data.kind.value : this.kind,
      workOrderId: data.workOrderId.present
          ? data.workOrderId.value
          : this.workOrderId,
      installationProjectId: data.installationProjectId.present
          ? data.installationProjectId.value
          : this.installationProjectId,
      latitude: data.latitude.present ? data.latitude.value : this.latitude,
      longitude: data.longitude.present ? data.longitude.value : this.longitude,
      capturedAt: data.capturedAt.present
          ? data.capturedAt.value
          : this.capturedAt,
      uploaded: data.uploaded.present ? data.uploaded.value : this.uploaded,
      failed: data.failed.present ? data.failed.value : this.failed,
      lastError: data.lastError.present ? data.lastError.value : this.lastError,
    );
  }

  @override
  String toString() {
    return (StringBuffer('PendingPhoto(')
          ..write('clientRef: $clientRef, ')
          ..write('localPath: $localPath, ')
          ..write('kind: $kind, ')
          ..write('workOrderId: $workOrderId, ')
          ..write('installationProjectId: $installationProjectId, ')
          ..write('latitude: $latitude, ')
          ..write('longitude: $longitude, ')
          ..write('capturedAt: $capturedAt, ')
          ..write('uploaded: $uploaded, ')
          ..write('failed: $failed, ')
          ..write('lastError: $lastError')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    clientRef,
    localPath,
    kind,
    workOrderId,
    installationProjectId,
    latitude,
    longitude,
    capturedAt,
    uploaded,
    failed,
    lastError,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is PendingPhoto &&
          other.clientRef == this.clientRef &&
          other.localPath == this.localPath &&
          other.kind == this.kind &&
          other.workOrderId == this.workOrderId &&
          other.installationProjectId == this.installationProjectId &&
          other.latitude == this.latitude &&
          other.longitude == this.longitude &&
          other.capturedAt == this.capturedAt &&
          other.uploaded == this.uploaded &&
          other.failed == this.failed &&
          other.lastError == this.lastError);
}

class PendingPhotosCompanion extends UpdateCompanion<PendingPhoto> {
  final Value<String> clientRef;
  final Value<String> localPath;
  final Value<String> kind;
  final Value<String?> workOrderId;
  final Value<String?> installationProjectId;
  final Value<double?> latitude;
  final Value<double?> longitude;
  final Value<DateTime> capturedAt;
  final Value<bool> uploaded;
  final Value<bool> failed;
  final Value<String?> lastError;
  final Value<int> rowid;
  const PendingPhotosCompanion({
    this.clientRef = const Value.absent(),
    this.localPath = const Value.absent(),
    this.kind = const Value.absent(),
    this.workOrderId = const Value.absent(),
    this.installationProjectId = const Value.absent(),
    this.latitude = const Value.absent(),
    this.longitude = const Value.absent(),
    this.capturedAt = const Value.absent(),
    this.uploaded = const Value.absent(),
    this.failed = const Value.absent(),
    this.lastError = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  PendingPhotosCompanion.insert({
    required String clientRef,
    required String localPath,
    this.kind = const Value.absent(),
    this.workOrderId = const Value.absent(),
    this.installationProjectId = const Value.absent(),
    this.latitude = const Value.absent(),
    this.longitude = const Value.absent(),
    required DateTime capturedAt,
    this.uploaded = const Value.absent(),
    this.failed = const Value.absent(),
    this.lastError = const Value.absent(),
    this.rowid = const Value.absent(),
  }) : clientRef = Value(clientRef),
       localPath = Value(localPath),
       capturedAt = Value(capturedAt);
  static Insertable<PendingPhoto> custom({
    Expression<String>? clientRef,
    Expression<String>? localPath,
    Expression<String>? kind,
    Expression<String>? workOrderId,
    Expression<String>? installationProjectId,
    Expression<double>? latitude,
    Expression<double>? longitude,
    Expression<DateTime>? capturedAt,
    Expression<bool>? uploaded,
    Expression<bool>? failed,
    Expression<String>? lastError,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (clientRef != null) 'client_ref': clientRef,
      if (localPath != null) 'local_path': localPath,
      if (kind != null) 'kind': kind,
      if (workOrderId != null) 'work_order_id': workOrderId,
      if (installationProjectId != null)
        'installation_project_id': installationProjectId,
      if (latitude != null) 'latitude': latitude,
      if (longitude != null) 'longitude': longitude,
      if (capturedAt != null) 'captured_at': capturedAt,
      if (uploaded != null) 'uploaded': uploaded,
      if (failed != null) 'failed': failed,
      if (lastError != null) 'last_error': lastError,
      if (rowid != null) 'rowid': rowid,
    });
  }

  PendingPhotosCompanion copyWith({
    Value<String>? clientRef,
    Value<String>? localPath,
    Value<String>? kind,
    Value<String?>? workOrderId,
    Value<String?>? installationProjectId,
    Value<double?>? latitude,
    Value<double?>? longitude,
    Value<DateTime>? capturedAt,
    Value<bool>? uploaded,
    Value<bool>? failed,
    Value<String?>? lastError,
    Value<int>? rowid,
  }) {
    return PendingPhotosCompanion(
      clientRef: clientRef ?? this.clientRef,
      localPath: localPath ?? this.localPath,
      kind: kind ?? this.kind,
      workOrderId: workOrderId ?? this.workOrderId,
      installationProjectId:
          installationProjectId ?? this.installationProjectId,
      latitude: latitude ?? this.latitude,
      longitude: longitude ?? this.longitude,
      capturedAt: capturedAt ?? this.capturedAt,
      uploaded: uploaded ?? this.uploaded,
      failed: failed ?? this.failed,
      lastError: lastError ?? this.lastError,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (clientRef.present) {
      map['client_ref'] = Variable<String>(clientRef.value);
    }
    if (localPath.present) {
      map['local_path'] = Variable<String>(localPath.value);
    }
    if (kind.present) {
      map['kind'] = Variable<String>(kind.value);
    }
    if (workOrderId.present) {
      map['work_order_id'] = Variable<String>(workOrderId.value);
    }
    if (installationProjectId.present) {
      map['installation_project_id'] = Variable<String>(
        installationProjectId.value,
      );
    }
    if (latitude.present) {
      map['latitude'] = Variable<double>(latitude.value);
    }
    if (longitude.present) {
      map['longitude'] = Variable<double>(longitude.value);
    }
    if (capturedAt.present) {
      map['captured_at'] = Variable<DateTime>(capturedAt.value);
    }
    if (uploaded.present) {
      map['uploaded'] = Variable<bool>(uploaded.value);
    }
    if (failed.present) {
      map['failed'] = Variable<bool>(failed.value);
    }
    if (lastError.present) {
      map['last_error'] = Variable<String>(lastError.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('PendingPhotosCompanion(')
          ..write('clientRef: $clientRef, ')
          ..write('localPath: $localPath, ')
          ..write('kind: $kind, ')
          ..write('workOrderId: $workOrderId, ')
          ..write('installationProjectId: $installationProjectId, ')
          ..write('latitude: $latitude, ')
          ..write('longitude: $longitude, ')
          ..write('capturedAt: $capturedAt, ')
          ..write('uploaded: $uploaded, ')
          ..write('failed: $failed, ')
          ..write('lastError: $lastError, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $DraftEntriesTable extends DraftEntries
    with TableInfo<$DraftEntriesTable, DraftEntry> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $DraftEntriesTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _idMeta = const VerificationMeta('id');
  @override
  late final GeneratedColumn<String> id = GeneratedColumn<String>(
    'id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _typeMeta = const VerificationMeta('type');
  @override
  late final GeneratedColumn<String> type = GeneratedColumn<String>(
    'type',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _payloadJsonMeta = const VerificationMeta(
    'payloadJson',
  );
  @override
  late final GeneratedColumn<String> payloadJson = GeneratedColumn<String>(
    'payload_json',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _updatedAtMeta = const VerificationMeta(
    'updatedAt',
  );
  @override
  late final GeneratedColumn<DateTime> updatedAt = GeneratedColumn<DateTime>(
    'updated_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [id, type, payloadJson, updatedAt];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'draft_entries';
  @override
  VerificationContext validateIntegrity(
    Insertable<DraftEntry> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('id')) {
      context.handle(_idMeta, id.isAcceptableOrUnknown(data['id']!, _idMeta));
    } else if (isInserting) {
      context.missing(_idMeta);
    }
    if (data.containsKey('type')) {
      context.handle(
        _typeMeta,
        type.isAcceptableOrUnknown(data['type']!, _typeMeta),
      );
    } else if (isInserting) {
      context.missing(_typeMeta);
    }
    if (data.containsKey('payload_json')) {
      context.handle(
        _payloadJsonMeta,
        payloadJson.isAcceptableOrUnknown(
          data['payload_json']!,
          _payloadJsonMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_payloadJsonMeta);
    }
    if (data.containsKey('updated_at')) {
      context.handle(
        _updatedAtMeta,
        updatedAt.isAcceptableOrUnknown(data['updated_at']!, _updatedAtMeta),
      );
    } else if (isInserting) {
      context.missing(_updatedAtMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {id};
  @override
  DraftEntry map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return DraftEntry(
      id: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}id'],
      )!,
      type: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}type'],
      )!,
      payloadJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}payload_json'],
      )!,
      updatedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}updated_at'],
      )!,
    );
  }

  @override
  $DraftEntriesTable createAlias(String alias) {
    return $DraftEntriesTable(attachedDatabase, alias);
  }
}

class DraftEntry extends DataClass implements Insertable<DraftEntry> {
  final String id;
  final String type;
  final String payloadJson;
  final DateTime updatedAt;
  const DraftEntry({
    required this.id,
    required this.type,
    required this.payloadJson,
    required this.updatedAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['id'] = Variable<String>(id);
    map['type'] = Variable<String>(type);
    map['payload_json'] = Variable<String>(payloadJson);
    map['updated_at'] = Variable<DateTime>(updatedAt);
    return map;
  }

  DraftEntriesCompanion toCompanion(bool nullToAbsent) {
    return DraftEntriesCompanion(
      id: Value(id),
      type: Value(type),
      payloadJson: Value(payloadJson),
      updatedAt: Value(updatedAt),
    );
  }

  factory DraftEntry.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return DraftEntry(
      id: serializer.fromJson<String>(json['id']),
      type: serializer.fromJson<String>(json['type']),
      payloadJson: serializer.fromJson<String>(json['payloadJson']),
      updatedAt: serializer.fromJson<DateTime>(json['updatedAt']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'id': serializer.toJson<String>(id),
      'type': serializer.toJson<String>(type),
      'payloadJson': serializer.toJson<String>(payloadJson),
      'updatedAt': serializer.toJson<DateTime>(updatedAt),
    };
  }

  DraftEntry copyWith({
    String? id,
    String? type,
    String? payloadJson,
    DateTime? updatedAt,
  }) => DraftEntry(
    id: id ?? this.id,
    type: type ?? this.type,
    payloadJson: payloadJson ?? this.payloadJson,
    updatedAt: updatedAt ?? this.updatedAt,
  );
  DraftEntry copyWithCompanion(DraftEntriesCompanion data) {
    return DraftEntry(
      id: data.id.present ? data.id.value : this.id,
      type: data.type.present ? data.type.value : this.type,
      payloadJson: data.payloadJson.present
          ? data.payloadJson.value
          : this.payloadJson,
      updatedAt: data.updatedAt.present ? data.updatedAt.value : this.updatedAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('DraftEntry(')
          ..write('id: $id, ')
          ..write('type: $type, ')
          ..write('payloadJson: $payloadJson, ')
          ..write('updatedAt: $updatedAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(id, type, payloadJson, updatedAt);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is DraftEntry &&
          other.id == this.id &&
          other.type == this.type &&
          other.payloadJson == this.payloadJson &&
          other.updatedAt == this.updatedAt);
}

class DraftEntriesCompanion extends UpdateCompanion<DraftEntry> {
  final Value<String> id;
  final Value<String> type;
  final Value<String> payloadJson;
  final Value<DateTime> updatedAt;
  final Value<int> rowid;
  const DraftEntriesCompanion({
    this.id = const Value.absent(),
    this.type = const Value.absent(),
    this.payloadJson = const Value.absent(),
    this.updatedAt = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  DraftEntriesCompanion.insert({
    required String id,
    required String type,
    required String payloadJson,
    required DateTime updatedAt,
    this.rowid = const Value.absent(),
  }) : id = Value(id),
       type = Value(type),
       payloadJson = Value(payloadJson),
       updatedAt = Value(updatedAt);
  static Insertable<DraftEntry> custom({
    Expression<String>? id,
    Expression<String>? type,
    Expression<String>? payloadJson,
    Expression<DateTime>? updatedAt,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (id != null) 'id': id,
      if (type != null) 'type': type,
      if (payloadJson != null) 'payload_json': payloadJson,
      if (updatedAt != null) 'updated_at': updatedAt,
      if (rowid != null) 'rowid': rowid,
    });
  }

  DraftEntriesCompanion copyWith({
    Value<String>? id,
    Value<String>? type,
    Value<String>? payloadJson,
    Value<DateTime>? updatedAt,
    Value<int>? rowid,
  }) {
    return DraftEntriesCompanion(
      id: id ?? this.id,
      type: type ?? this.type,
      payloadJson: payloadJson ?? this.payloadJson,
      updatedAt: updatedAt ?? this.updatedAt,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (id.present) {
      map['id'] = Variable<String>(id.value);
    }
    if (type.present) {
      map['type'] = Variable<String>(type.value);
    }
    if (payloadJson.present) {
      map['payload_json'] = Variable<String>(payloadJson.value);
    }
    if (updatedAt.present) {
      map['updated_at'] = Variable<DateTime>(updatedAt.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('DraftEntriesCompanion(')
          ..write('id: $id, ')
          ..write('type: $type, ')
          ..write('payloadJson: $payloadJson, ')
          ..write('updatedAt: $updatedAt, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

abstract class _$AppDatabase extends GeneratedDatabase {
  _$AppDatabase(QueryExecutor e) : super(e);
  $AppDatabaseManager get managers => $AppDatabaseManager(this);
  late final $CachedJobsTable cachedJobs = $CachedJobsTable(this);
  late final $CachedScheduleEntriesTable cachedScheduleEntries =
      $CachedScheduleEntriesTable(this);
  late final $CachedMapAssetsTable cachedMapAssets = $CachedMapAssetsTable(
    this,
  );
  late final $CachedMapAssetSyncCursorsTable cachedMapAssetSyncCursors =
      $CachedMapAssetSyncCursorsTable(this);
  late final $OutboxEntriesTable outboxEntries = $OutboxEntriesTable(this);
  late final $PendingPhotosTable pendingPhotos = $PendingPhotosTable(this);
  late final $DraftEntriesTable draftEntries = $DraftEntriesTable(this);
  @override
  Iterable<TableInfo<Table, Object?>> get allTables =>
      allSchemaEntities.whereType<TableInfo<Table, Object?>>();
  @override
  List<DatabaseSchemaEntity> get allSchemaEntities => [
    cachedJobs,
    cachedScheduleEntries,
    cachedMapAssets,
    cachedMapAssetSyncCursors,
    outboxEntries,
    pendingPhotos,
    draftEntries,
  ];
}

typedef $$CachedJobsTableCreateCompanionBuilder =
    CachedJobsCompanion Function({
      required String id,
      required String title,
      required String status,
      required String workType,
      required String priority,
      Value<DateTime?> scheduledStart,
      Value<String?> detailJson,
      required DateTime cachedAt,
      Value<int> rowid,
    });
typedef $$CachedJobsTableUpdateCompanionBuilder =
    CachedJobsCompanion Function({
      Value<String> id,
      Value<String> title,
      Value<String> status,
      Value<String> workType,
      Value<String> priority,
      Value<DateTime?> scheduledStart,
      Value<String?> detailJson,
      Value<DateTime> cachedAt,
      Value<int> rowid,
    });

class $$CachedJobsTableFilterComposer
    extends Composer<_$AppDatabase, $CachedJobsTable> {
  $$CachedJobsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get title => $composableBuilder(
    column: $table.title,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get status => $composableBuilder(
    column: $table.status,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get workType => $composableBuilder(
    column: $table.workType,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get priority => $composableBuilder(
    column: $table.priority,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get scheduledStart => $composableBuilder(
    column: $table.scheduledStart,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get detailJson => $composableBuilder(
    column: $table.detailJson,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get cachedAt => $composableBuilder(
    column: $table.cachedAt,
    builder: (column) => ColumnFilters(column),
  );
}

class $$CachedJobsTableOrderingComposer
    extends Composer<_$AppDatabase, $CachedJobsTable> {
  $$CachedJobsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get title => $composableBuilder(
    column: $table.title,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get status => $composableBuilder(
    column: $table.status,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get workType => $composableBuilder(
    column: $table.workType,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get priority => $composableBuilder(
    column: $table.priority,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get scheduledStart => $composableBuilder(
    column: $table.scheduledStart,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get detailJson => $composableBuilder(
    column: $table.detailJson,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get cachedAt => $composableBuilder(
    column: $table.cachedAt,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$CachedJobsTableAnnotationComposer
    extends Composer<_$AppDatabase, $CachedJobsTable> {
  $$CachedJobsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get id =>
      $composableBuilder(column: $table.id, builder: (column) => column);

  GeneratedColumn<String> get title =>
      $composableBuilder(column: $table.title, builder: (column) => column);

  GeneratedColumn<String> get status =>
      $composableBuilder(column: $table.status, builder: (column) => column);

  GeneratedColumn<String> get workType =>
      $composableBuilder(column: $table.workType, builder: (column) => column);

  GeneratedColumn<String> get priority =>
      $composableBuilder(column: $table.priority, builder: (column) => column);

  GeneratedColumn<DateTime> get scheduledStart => $composableBuilder(
    column: $table.scheduledStart,
    builder: (column) => column,
  );

  GeneratedColumn<String> get detailJson => $composableBuilder(
    column: $table.detailJson,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get cachedAt =>
      $composableBuilder(column: $table.cachedAt, builder: (column) => column);
}

class $$CachedJobsTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $CachedJobsTable,
          CachedJob,
          $$CachedJobsTableFilterComposer,
          $$CachedJobsTableOrderingComposer,
          $$CachedJobsTableAnnotationComposer,
          $$CachedJobsTableCreateCompanionBuilder,
          $$CachedJobsTableUpdateCompanionBuilder,
          (
            CachedJob,
            BaseReferences<_$AppDatabase, $CachedJobsTable, CachedJob>,
          ),
          CachedJob,
          PrefetchHooks Function()
        > {
  $$CachedJobsTableTableManager(_$AppDatabase db, $CachedJobsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$CachedJobsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$CachedJobsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$CachedJobsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> id = const Value.absent(),
                Value<String> title = const Value.absent(),
                Value<String> status = const Value.absent(),
                Value<String> workType = const Value.absent(),
                Value<String> priority = const Value.absent(),
                Value<DateTime?> scheduledStart = const Value.absent(),
                Value<String?> detailJson = const Value.absent(),
                Value<DateTime> cachedAt = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => CachedJobsCompanion(
                id: id,
                title: title,
                status: status,
                workType: workType,
                priority: priority,
                scheduledStart: scheduledStart,
                detailJson: detailJson,
                cachedAt: cachedAt,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String id,
                required String title,
                required String status,
                required String workType,
                required String priority,
                Value<DateTime?> scheduledStart = const Value.absent(),
                Value<String?> detailJson = const Value.absent(),
                required DateTime cachedAt,
                Value<int> rowid = const Value.absent(),
              }) => CachedJobsCompanion.insert(
                id: id,
                title: title,
                status: status,
                workType: workType,
                priority: priority,
                scheduledStart: scheduledStart,
                detailJson: detailJson,
                cachedAt: cachedAt,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$CachedJobsTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $CachedJobsTable,
      CachedJob,
      $$CachedJobsTableFilterComposer,
      $$CachedJobsTableOrderingComposer,
      $$CachedJobsTableAnnotationComposer,
      $$CachedJobsTableCreateCompanionBuilder,
      $$CachedJobsTableUpdateCompanionBuilder,
      (CachedJob, BaseReferences<_$AppDatabase, $CachedJobsTable, CachedJob>),
      CachedJob,
      PrefetchHooks Function()
    >;
typedef $$CachedScheduleEntriesTableCreateCompanionBuilder =
    CachedScheduleEntriesCompanion Function({
      required String referenceId,
      required String type,
      required DateTime startAt,
      Value<DateTime?> endAt,
      required String title,
      Value<int> rowid,
    });
typedef $$CachedScheduleEntriesTableUpdateCompanionBuilder =
    CachedScheduleEntriesCompanion Function({
      Value<String> referenceId,
      Value<String> type,
      Value<DateTime> startAt,
      Value<DateTime?> endAt,
      Value<String> title,
      Value<int> rowid,
    });

class $$CachedScheduleEntriesTableFilterComposer
    extends Composer<_$AppDatabase, $CachedScheduleEntriesTable> {
  $$CachedScheduleEntriesTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get referenceId => $composableBuilder(
    column: $table.referenceId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get type => $composableBuilder(
    column: $table.type,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get startAt => $composableBuilder(
    column: $table.startAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get endAt => $composableBuilder(
    column: $table.endAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get title => $composableBuilder(
    column: $table.title,
    builder: (column) => ColumnFilters(column),
  );
}

class $$CachedScheduleEntriesTableOrderingComposer
    extends Composer<_$AppDatabase, $CachedScheduleEntriesTable> {
  $$CachedScheduleEntriesTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get referenceId => $composableBuilder(
    column: $table.referenceId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get type => $composableBuilder(
    column: $table.type,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get startAt => $composableBuilder(
    column: $table.startAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get endAt => $composableBuilder(
    column: $table.endAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get title => $composableBuilder(
    column: $table.title,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$CachedScheduleEntriesTableAnnotationComposer
    extends Composer<_$AppDatabase, $CachedScheduleEntriesTable> {
  $$CachedScheduleEntriesTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get referenceId => $composableBuilder(
    column: $table.referenceId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get type =>
      $composableBuilder(column: $table.type, builder: (column) => column);

  GeneratedColumn<DateTime> get startAt =>
      $composableBuilder(column: $table.startAt, builder: (column) => column);

  GeneratedColumn<DateTime> get endAt =>
      $composableBuilder(column: $table.endAt, builder: (column) => column);

  GeneratedColumn<String> get title =>
      $composableBuilder(column: $table.title, builder: (column) => column);
}

class $$CachedScheduleEntriesTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $CachedScheduleEntriesTable,
          CachedScheduleEntry,
          $$CachedScheduleEntriesTableFilterComposer,
          $$CachedScheduleEntriesTableOrderingComposer,
          $$CachedScheduleEntriesTableAnnotationComposer,
          $$CachedScheduleEntriesTableCreateCompanionBuilder,
          $$CachedScheduleEntriesTableUpdateCompanionBuilder,
          (
            CachedScheduleEntry,
            BaseReferences<
              _$AppDatabase,
              $CachedScheduleEntriesTable,
              CachedScheduleEntry
            >,
          ),
          CachedScheduleEntry,
          PrefetchHooks Function()
        > {
  $$CachedScheduleEntriesTableTableManager(
    _$AppDatabase db,
    $CachedScheduleEntriesTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$CachedScheduleEntriesTableFilterComposer(
                $db: db,
                $table: table,
              ),
          createOrderingComposer: () =>
              $$CachedScheduleEntriesTableOrderingComposer(
                $db: db,
                $table: table,
              ),
          createComputedFieldComposer: () =>
              $$CachedScheduleEntriesTableAnnotationComposer(
                $db: db,
                $table: table,
              ),
          updateCompanionCallback:
              ({
                Value<String> referenceId = const Value.absent(),
                Value<String> type = const Value.absent(),
                Value<DateTime> startAt = const Value.absent(),
                Value<DateTime?> endAt = const Value.absent(),
                Value<String> title = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => CachedScheduleEntriesCompanion(
                referenceId: referenceId,
                type: type,
                startAt: startAt,
                endAt: endAt,
                title: title,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String referenceId,
                required String type,
                required DateTime startAt,
                Value<DateTime?> endAt = const Value.absent(),
                required String title,
                Value<int> rowid = const Value.absent(),
              }) => CachedScheduleEntriesCompanion.insert(
                referenceId: referenceId,
                type: type,
                startAt: startAt,
                endAt: endAt,
                title: title,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$CachedScheduleEntriesTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $CachedScheduleEntriesTable,
      CachedScheduleEntry,
      $$CachedScheduleEntriesTableFilterComposer,
      $$CachedScheduleEntriesTableOrderingComposer,
      $$CachedScheduleEntriesTableAnnotationComposer,
      $$CachedScheduleEntriesTableCreateCompanionBuilder,
      $$CachedScheduleEntriesTableUpdateCompanionBuilder,
      (
        CachedScheduleEntry,
        BaseReferences<
          _$AppDatabase,
          $CachedScheduleEntriesTable,
          CachedScheduleEntry
        >,
      ),
      CachedScheduleEntry,
      PrefetchHooks Function()
    >;
typedef $$CachedMapAssetsTableCreateCompanionBuilder =
    CachedMapAssetsCompanion Function({
      required String assetType,
      required String assetId,
      required String title,
      Value<String?> subtitle,
      required double latitude,
      required double longitude,
      Value<String?> status,
      Value<DateTime?> updatedAt,
      required DateTime cachedAt,
      Value<int> rowid,
    });
typedef $$CachedMapAssetsTableUpdateCompanionBuilder =
    CachedMapAssetsCompanion Function({
      Value<String> assetType,
      Value<String> assetId,
      Value<String> title,
      Value<String?> subtitle,
      Value<double> latitude,
      Value<double> longitude,
      Value<String?> status,
      Value<DateTime?> updatedAt,
      Value<DateTime> cachedAt,
      Value<int> rowid,
    });

class $$CachedMapAssetsTableFilterComposer
    extends Composer<_$AppDatabase, $CachedMapAssetsTable> {
  $$CachedMapAssetsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get assetType => $composableBuilder(
    column: $table.assetType,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get assetId => $composableBuilder(
    column: $table.assetId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get title => $composableBuilder(
    column: $table.title,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get subtitle => $composableBuilder(
    column: $table.subtitle,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<double> get latitude => $composableBuilder(
    column: $table.latitude,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<double> get longitude => $composableBuilder(
    column: $table.longitude,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get status => $composableBuilder(
    column: $table.status,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get cachedAt => $composableBuilder(
    column: $table.cachedAt,
    builder: (column) => ColumnFilters(column),
  );
}

class $$CachedMapAssetsTableOrderingComposer
    extends Composer<_$AppDatabase, $CachedMapAssetsTable> {
  $$CachedMapAssetsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get assetType => $composableBuilder(
    column: $table.assetType,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get assetId => $composableBuilder(
    column: $table.assetId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get title => $composableBuilder(
    column: $table.title,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get subtitle => $composableBuilder(
    column: $table.subtitle,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<double> get latitude => $composableBuilder(
    column: $table.latitude,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<double> get longitude => $composableBuilder(
    column: $table.longitude,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get status => $composableBuilder(
    column: $table.status,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get cachedAt => $composableBuilder(
    column: $table.cachedAt,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$CachedMapAssetsTableAnnotationComposer
    extends Composer<_$AppDatabase, $CachedMapAssetsTable> {
  $$CachedMapAssetsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get assetType =>
      $composableBuilder(column: $table.assetType, builder: (column) => column);

  GeneratedColumn<String> get assetId =>
      $composableBuilder(column: $table.assetId, builder: (column) => column);

  GeneratedColumn<String> get title =>
      $composableBuilder(column: $table.title, builder: (column) => column);

  GeneratedColumn<String> get subtitle =>
      $composableBuilder(column: $table.subtitle, builder: (column) => column);

  GeneratedColumn<double> get latitude =>
      $composableBuilder(column: $table.latitude, builder: (column) => column);

  GeneratedColumn<double> get longitude =>
      $composableBuilder(column: $table.longitude, builder: (column) => column);

  GeneratedColumn<String> get status =>
      $composableBuilder(column: $table.status, builder: (column) => column);

  GeneratedColumn<DateTime> get updatedAt =>
      $composableBuilder(column: $table.updatedAt, builder: (column) => column);

  GeneratedColumn<DateTime> get cachedAt =>
      $composableBuilder(column: $table.cachedAt, builder: (column) => column);
}

class $$CachedMapAssetsTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $CachedMapAssetsTable,
          CachedMapAsset,
          $$CachedMapAssetsTableFilterComposer,
          $$CachedMapAssetsTableOrderingComposer,
          $$CachedMapAssetsTableAnnotationComposer,
          $$CachedMapAssetsTableCreateCompanionBuilder,
          $$CachedMapAssetsTableUpdateCompanionBuilder,
          (
            CachedMapAsset,
            BaseReferences<
              _$AppDatabase,
              $CachedMapAssetsTable,
              CachedMapAsset
            >,
          ),
          CachedMapAsset,
          PrefetchHooks Function()
        > {
  $$CachedMapAssetsTableTableManager(
    _$AppDatabase db,
    $CachedMapAssetsTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$CachedMapAssetsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$CachedMapAssetsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$CachedMapAssetsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> assetType = const Value.absent(),
                Value<String> assetId = const Value.absent(),
                Value<String> title = const Value.absent(),
                Value<String?> subtitle = const Value.absent(),
                Value<double> latitude = const Value.absent(),
                Value<double> longitude = const Value.absent(),
                Value<String?> status = const Value.absent(),
                Value<DateTime?> updatedAt = const Value.absent(),
                Value<DateTime> cachedAt = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => CachedMapAssetsCompanion(
                assetType: assetType,
                assetId: assetId,
                title: title,
                subtitle: subtitle,
                latitude: latitude,
                longitude: longitude,
                status: status,
                updatedAt: updatedAt,
                cachedAt: cachedAt,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String assetType,
                required String assetId,
                required String title,
                Value<String?> subtitle = const Value.absent(),
                required double latitude,
                required double longitude,
                Value<String?> status = const Value.absent(),
                Value<DateTime?> updatedAt = const Value.absent(),
                required DateTime cachedAt,
                Value<int> rowid = const Value.absent(),
              }) => CachedMapAssetsCompanion.insert(
                assetType: assetType,
                assetId: assetId,
                title: title,
                subtitle: subtitle,
                latitude: latitude,
                longitude: longitude,
                status: status,
                updatedAt: updatedAt,
                cachedAt: cachedAt,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$CachedMapAssetsTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $CachedMapAssetsTable,
      CachedMapAsset,
      $$CachedMapAssetsTableFilterComposer,
      $$CachedMapAssetsTableOrderingComposer,
      $$CachedMapAssetsTableAnnotationComposer,
      $$CachedMapAssetsTableCreateCompanionBuilder,
      $$CachedMapAssetsTableUpdateCompanionBuilder,
      (
        CachedMapAsset,
        BaseReferences<_$AppDatabase, $CachedMapAssetsTable, CachedMapAsset>,
      ),
      CachedMapAsset,
      PrefetchHooks Function()
    >;
typedef $$CachedMapAssetSyncCursorsTableCreateCompanionBuilder =
    CachedMapAssetSyncCursorsCompanion Function({
      required String assetType,
      required DateTime syncedAt,
      Value<int> rowid,
    });
typedef $$CachedMapAssetSyncCursorsTableUpdateCompanionBuilder =
    CachedMapAssetSyncCursorsCompanion Function({
      Value<String> assetType,
      Value<DateTime> syncedAt,
      Value<int> rowid,
    });

class $$CachedMapAssetSyncCursorsTableFilterComposer
    extends Composer<_$AppDatabase, $CachedMapAssetSyncCursorsTable> {
  $$CachedMapAssetSyncCursorsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get assetType => $composableBuilder(
    column: $table.assetType,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get syncedAt => $composableBuilder(
    column: $table.syncedAt,
    builder: (column) => ColumnFilters(column),
  );
}

class $$CachedMapAssetSyncCursorsTableOrderingComposer
    extends Composer<_$AppDatabase, $CachedMapAssetSyncCursorsTable> {
  $$CachedMapAssetSyncCursorsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get assetType => $composableBuilder(
    column: $table.assetType,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get syncedAt => $composableBuilder(
    column: $table.syncedAt,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$CachedMapAssetSyncCursorsTableAnnotationComposer
    extends Composer<_$AppDatabase, $CachedMapAssetSyncCursorsTable> {
  $$CachedMapAssetSyncCursorsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get assetType =>
      $composableBuilder(column: $table.assetType, builder: (column) => column);

  GeneratedColumn<DateTime> get syncedAt =>
      $composableBuilder(column: $table.syncedAt, builder: (column) => column);
}

class $$CachedMapAssetSyncCursorsTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $CachedMapAssetSyncCursorsTable,
          CachedMapAssetSyncCursor,
          $$CachedMapAssetSyncCursorsTableFilterComposer,
          $$CachedMapAssetSyncCursorsTableOrderingComposer,
          $$CachedMapAssetSyncCursorsTableAnnotationComposer,
          $$CachedMapAssetSyncCursorsTableCreateCompanionBuilder,
          $$CachedMapAssetSyncCursorsTableUpdateCompanionBuilder,
          (
            CachedMapAssetSyncCursor,
            BaseReferences<
              _$AppDatabase,
              $CachedMapAssetSyncCursorsTable,
              CachedMapAssetSyncCursor
            >,
          ),
          CachedMapAssetSyncCursor,
          PrefetchHooks Function()
        > {
  $$CachedMapAssetSyncCursorsTableTableManager(
    _$AppDatabase db,
    $CachedMapAssetSyncCursorsTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$CachedMapAssetSyncCursorsTableFilterComposer(
                $db: db,
                $table: table,
              ),
          createOrderingComposer: () =>
              $$CachedMapAssetSyncCursorsTableOrderingComposer(
                $db: db,
                $table: table,
              ),
          createComputedFieldComposer: () =>
              $$CachedMapAssetSyncCursorsTableAnnotationComposer(
                $db: db,
                $table: table,
              ),
          updateCompanionCallback:
              ({
                Value<String> assetType = const Value.absent(),
                Value<DateTime> syncedAt = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => CachedMapAssetSyncCursorsCompanion(
                assetType: assetType,
                syncedAt: syncedAt,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String assetType,
                required DateTime syncedAt,
                Value<int> rowid = const Value.absent(),
              }) => CachedMapAssetSyncCursorsCompanion.insert(
                assetType: assetType,
                syncedAt: syncedAt,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$CachedMapAssetSyncCursorsTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $CachedMapAssetSyncCursorsTable,
      CachedMapAssetSyncCursor,
      $$CachedMapAssetSyncCursorsTableFilterComposer,
      $$CachedMapAssetSyncCursorsTableOrderingComposer,
      $$CachedMapAssetSyncCursorsTableAnnotationComposer,
      $$CachedMapAssetSyncCursorsTableCreateCompanionBuilder,
      $$CachedMapAssetSyncCursorsTableUpdateCompanionBuilder,
      (
        CachedMapAssetSyncCursor,
        BaseReferences<
          _$AppDatabase,
          $CachedMapAssetSyncCursorsTable,
          CachedMapAssetSyncCursor
        >,
      ),
      CachedMapAssetSyncCursor,
      PrefetchHooks Function()
    >;
typedef $$OutboxEntriesTableCreateCompanionBuilder =
    OutboxEntriesCompanion Function({
      Value<int> seq,
      required String clientRef,
      required String kind,
      required String payloadJson,
      Value<String> status,
      Value<int> attempts,
      Value<String?> lastError,
      required DateTime createdAt,
    });
typedef $$OutboxEntriesTableUpdateCompanionBuilder =
    OutboxEntriesCompanion Function({
      Value<int> seq,
      Value<String> clientRef,
      Value<String> kind,
      Value<String> payloadJson,
      Value<String> status,
      Value<int> attempts,
      Value<String?> lastError,
      Value<DateTime> createdAt,
    });

class $$OutboxEntriesTableFilterComposer
    extends Composer<_$AppDatabase, $OutboxEntriesTable> {
  $$OutboxEntriesTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<int> get seq => $composableBuilder(
    column: $table.seq,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get clientRef => $composableBuilder(
    column: $table.clientRef,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get kind => $composableBuilder(
    column: $table.kind,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get payloadJson => $composableBuilder(
    column: $table.payloadJson,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get status => $composableBuilder(
    column: $table.status,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get attempts => $composableBuilder(
    column: $table.attempts,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get lastError => $composableBuilder(
    column: $table.lastError,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get createdAt => $composableBuilder(
    column: $table.createdAt,
    builder: (column) => ColumnFilters(column),
  );
}

class $$OutboxEntriesTableOrderingComposer
    extends Composer<_$AppDatabase, $OutboxEntriesTable> {
  $$OutboxEntriesTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<int> get seq => $composableBuilder(
    column: $table.seq,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get clientRef => $composableBuilder(
    column: $table.clientRef,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get kind => $composableBuilder(
    column: $table.kind,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get payloadJson => $composableBuilder(
    column: $table.payloadJson,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get status => $composableBuilder(
    column: $table.status,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get attempts => $composableBuilder(
    column: $table.attempts,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get lastError => $composableBuilder(
    column: $table.lastError,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get createdAt => $composableBuilder(
    column: $table.createdAt,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$OutboxEntriesTableAnnotationComposer
    extends Composer<_$AppDatabase, $OutboxEntriesTable> {
  $$OutboxEntriesTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<int> get seq =>
      $composableBuilder(column: $table.seq, builder: (column) => column);

  GeneratedColumn<String> get clientRef =>
      $composableBuilder(column: $table.clientRef, builder: (column) => column);

  GeneratedColumn<String> get kind =>
      $composableBuilder(column: $table.kind, builder: (column) => column);

  GeneratedColumn<String> get payloadJson => $composableBuilder(
    column: $table.payloadJson,
    builder: (column) => column,
  );

  GeneratedColumn<String> get status =>
      $composableBuilder(column: $table.status, builder: (column) => column);

  GeneratedColumn<int> get attempts =>
      $composableBuilder(column: $table.attempts, builder: (column) => column);

  GeneratedColumn<String> get lastError =>
      $composableBuilder(column: $table.lastError, builder: (column) => column);

  GeneratedColumn<DateTime> get createdAt =>
      $composableBuilder(column: $table.createdAt, builder: (column) => column);
}

class $$OutboxEntriesTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $OutboxEntriesTable,
          OutboxEntry,
          $$OutboxEntriesTableFilterComposer,
          $$OutboxEntriesTableOrderingComposer,
          $$OutboxEntriesTableAnnotationComposer,
          $$OutboxEntriesTableCreateCompanionBuilder,
          $$OutboxEntriesTableUpdateCompanionBuilder,
          (
            OutboxEntry,
            BaseReferences<_$AppDatabase, $OutboxEntriesTable, OutboxEntry>,
          ),
          OutboxEntry,
          PrefetchHooks Function()
        > {
  $$OutboxEntriesTableTableManager(_$AppDatabase db, $OutboxEntriesTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$OutboxEntriesTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$OutboxEntriesTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$OutboxEntriesTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<int> seq = const Value.absent(),
                Value<String> clientRef = const Value.absent(),
                Value<String> kind = const Value.absent(),
                Value<String> payloadJson = const Value.absent(),
                Value<String> status = const Value.absent(),
                Value<int> attempts = const Value.absent(),
                Value<String?> lastError = const Value.absent(),
                Value<DateTime> createdAt = const Value.absent(),
              }) => OutboxEntriesCompanion(
                seq: seq,
                clientRef: clientRef,
                kind: kind,
                payloadJson: payloadJson,
                status: status,
                attempts: attempts,
                lastError: lastError,
                createdAt: createdAt,
              ),
          createCompanionCallback:
              ({
                Value<int> seq = const Value.absent(),
                required String clientRef,
                required String kind,
                required String payloadJson,
                Value<String> status = const Value.absent(),
                Value<int> attempts = const Value.absent(),
                Value<String?> lastError = const Value.absent(),
                required DateTime createdAt,
              }) => OutboxEntriesCompanion.insert(
                seq: seq,
                clientRef: clientRef,
                kind: kind,
                payloadJson: payloadJson,
                status: status,
                attempts: attempts,
                lastError: lastError,
                createdAt: createdAt,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$OutboxEntriesTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $OutboxEntriesTable,
      OutboxEntry,
      $$OutboxEntriesTableFilterComposer,
      $$OutboxEntriesTableOrderingComposer,
      $$OutboxEntriesTableAnnotationComposer,
      $$OutboxEntriesTableCreateCompanionBuilder,
      $$OutboxEntriesTableUpdateCompanionBuilder,
      (
        OutboxEntry,
        BaseReferences<_$AppDatabase, $OutboxEntriesTable, OutboxEntry>,
      ),
      OutboxEntry,
      PrefetchHooks Function()
    >;
typedef $$PendingPhotosTableCreateCompanionBuilder =
    PendingPhotosCompanion Function({
      required String clientRef,
      required String localPath,
      Value<String> kind,
      Value<String?> workOrderId,
      Value<String?> installationProjectId,
      Value<double?> latitude,
      Value<double?> longitude,
      required DateTime capturedAt,
      Value<bool> uploaded,
      Value<bool> failed,
      Value<String?> lastError,
      Value<int> rowid,
    });
typedef $$PendingPhotosTableUpdateCompanionBuilder =
    PendingPhotosCompanion Function({
      Value<String> clientRef,
      Value<String> localPath,
      Value<String> kind,
      Value<String?> workOrderId,
      Value<String?> installationProjectId,
      Value<double?> latitude,
      Value<double?> longitude,
      Value<DateTime> capturedAt,
      Value<bool> uploaded,
      Value<bool> failed,
      Value<String?> lastError,
      Value<int> rowid,
    });

class $$PendingPhotosTableFilterComposer
    extends Composer<_$AppDatabase, $PendingPhotosTable> {
  $$PendingPhotosTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get clientRef => $composableBuilder(
    column: $table.clientRef,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get localPath => $composableBuilder(
    column: $table.localPath,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get kind => $composableBuilder(
    column: $table.kind,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get workOrderId => $composableBuilder(
    column: $table.workOrderId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get installationProjectId => $composableBuilder(
    column: $table.installationProjectId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<double> get latitude => $composableBuilder(
    column: $table.latitude,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<double> get longitude => $composableBuilder(
    column: $table.longitude,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get capturedAt => $composableBuilder(
    column: $table.capturedAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get uploaded => $composableBuilder(
    column: $table.uploaded,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get failed => $composableBuilder(
    column: $table.failed,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get lastError => $composableBuilder(
    column: $table.lastError,
    builder: (column) => ColumnFilters(column),
  );
}

class $$PendingPhotosTableOrderingComposer
    extends Composer<_$AppDatabase, $PendingPhotosTable> {
  $$PendingPhotosTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get clientRef => $composableBuilder(
    column: $table.clientRef,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get localPath => $composableBuilder(
    column: $table.localPath,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get kind => $composableBuilder(
    column: $table.kind,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get workOrderId => $composableBuilder(
    column: $table.workOrderId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get installationProjectId => $composableBuilder(
    column: $table.installationProjectId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<double> get latitude => $composableBuilder(
    column: $table.latitude,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<double> get longitude => $composableBuilder(
    column: $table.longitude,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get capturedAt => $composableBuilder(
    column: $table.capturedAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get uploaded => $composableBuilder(
    column: $table.uploaded,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get failed => $composableBuilder(
    column: $table.failed,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get lastError => $composableBuilder(
    column: $table.lastError,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$PendingPhotosTableAnnotationComposer
    extends Composer<_$AppDatabase, $PendingPhotosTable> {
  $$PendingPhotosTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get clientRef =>
      $composableBuilder(column: $table.clientRef, builder: (column) => column);

  GeneratedColumn<String> get localPath =>
      $composableBuilder(column: $table.localPath, builder: (column) => column);

  GeneratedColumn<String> get kind =>
      $composableBuilder(column: $table.kind, builder: (column) => column);

  GeneratedColumn<String> get workOrderId => $composableBuilder(
    column: $table.workOrderId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get installationProjectId => $composableBuilder(
    column: $table.installationProjectId,
    builder: (column) => column,
  );

  GeneratedColumn<double> get latitude =>
      $composableBuilder(column: $table.latitude, builder: (column) => column);

  GeneratedColumn<double> get longitude =>
      $composableBuilder(column: $table.longitude, builder: (column) => column);

  GeneratedColumn<DateTime> get capturedAt => $composableBuilder(
    column: $table.capturedAt,
    builder: (column) => column,
  );

  GeneratedColumn<bool> get uploaded =>
      $composableBuilder(column: $table.uploaded, builder: (column) => column);

  GeneratedColumn<bool> get failed =>
      $composableBuilder(column: $table.failed, builder: (column) => column);

  GeneratedColumn<String> get lastError =>
      $composableBuilder(column: $table.lastError, builder: (column) => column);
}

class $$PendingPhotosTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $PendingPhotosTable,
          PendingPhoto,
          $$PendingPhotosTableFilterComposer,
          $$PendingPhotosTableOrderingComposer,
          $$PendingPhotosTableAnnotationComposer,
          $$PendingPhotosTableCreateCompanionBuilder,
          $$PendingPhotosTableUpdateCompanionBuilder,
          (
            PendingPhoto,
            BaseReferences<_$AppDatabase, $PendingPhotosTable, PendingPhoto>,
          ),
          PendingPhoto,
          PrefetchHooks Function()
        > {
  $$PendingPhotosTableTableManager(_$AppDatabase db, $PendingPhotosTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$PendingPhotosTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$PendingPhotosTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$PendingPhotosTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> clientRef = const Value.absent(),
                Value<String> localPath = const Value.absent(),
                Value<String> kind = const Value.absent(),
                Value<String?> workOrderId = const Value.absent(),
                Value<String?> installationProjectId = const Value.absent(),
                Value<double?> latitude = const Value.absent(),
                Value<double?> longitude = const Value.absent(),
                Value<DateTime> capturedAt = const Value.absent(),
                Value<bool> uploaded = const Value.absent(),
                Value<bool> failed = const Value.absent(),
                Value<String?> lastError = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => PendingPhotosCompanion(
                clientRef: clientRef,
                localPath: localPath,
                kind: kind,
                workOrderId: workOrderId,
                installationProjectId: installationProjectId,
                latitude: latitude,
                longitude: longitude,
                capturedAt: capturedAt,
                uploaded: uploaded,
                failed: failed,
                lastError: lastError,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String clientRef,
                required String localPath,
                Value<String> kind = const Value.absent(),
                Value<String?> workOrderId = const Value.absent(),
                Value<String?> installationProjectId = const Value.absent(),
                Value<double?> latitude = const Value.absent(),
                Value<double?> longitude = const Value.absent(),
                required DateTime capturedAt,
                Value<bool> uploaded = const Value.absent(),
                Value<bool> failed = const Value.absent(),
                Value<String?> lastError = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => PendingPhotosCompanion.insert(
                clientRef: clientRef,
                localPath: localPath,
                kind: kind,
                workOrderId: workOrderId,
                installationProjectId: installationProjectId,
                latitude: latitude,
                longitude: longitude,
                capturedAt: capturedAt,
                uploaded: uploaded,
                failed: failed,
                lastError: lastError,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$PendingPhotosTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $PendingPhotosTable,
      PendingPhoto,
      $$PendingPhotosTableFilterComposer,
      $$PendingPhotosTableOrderingComposer,
      $$PendingPhotosTableAnnotationComposer,
      $$PendingPhotosTableCreateCompanionBuilder,
      $$PendingPhotosTableUpdateCompanionBuilder,
      (
        PendingPhoto,
        BaseReferences<_$AppDatabase, $PendingPhotosTable, PendingPhoto>,
      ),
      PendingPhoto,
      PrefetchHooks Function()
    >;
typedef $$DraftEntriesTableCreateCompanionBuilder =
    DraftEntriesCompanion Function({
      required String id,
      required String type,
      required String payloadJson,
      required DateTime updatedAt,
      Value<int> rowid,
    });
typedef $$DraftEntriesTableUpdateCompanionBuilder =
    DraftEntriesCompanion Function({
      Value<String> id,
      Value<String> type,
      Value<String> payloadJson,
      Value<DateTime> updatedAt,
      Value<int> rowid,
    });

class $$DraftEntriesTableFilterComposer
    extends Composer<_$AppDatabase, $DraftEntriesTable> {
  $$DraftEntriesTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get type => $composableBuilder(
    column: $table.type,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get payloadJson => $composableBuilder(
    column: $table.payloadJson,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnFilters(column),
  );
}

class $$DraftEntriesTableOrderingComposer
    extends Composer<_$AppDatabase, $DraftEntriesTable> {
  $$DraftEntriesTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get type => $composableBuilder(
    column: $table.type,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get payloadJson => $composableBuilder(
    column: $table.payloadJson,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$DraftEntriesTableAnnotationComposer
    extends Composer<_$AppDatabase, $DraftEntriesTable> {
  $$DraftEntriesTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get id =>
      $composableBuilder(column: $table.id, builder: (column) => column);

  GeneratedColumn<String> get type =>
      $composableBuilder(column: $table.type, builder: (column) => column);

  GeneratedColumn<String> get payloadJson => $composableBuilder(
    column: $table.payloadJson,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get updatedAt =>
      $composableBuilder(column: $table.updatedAt, builder: (column) => column);
}

class $$DraftEntriesTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $DraftEntriesTable,
          DraftEntry,
          $$DraftEntriesTableFilterComposer,
          $$DraftEntriesTableOrderingComposer,
          $$DraftEntriesTableAnnotationComposer,
          $$DraftEntriesTableCreateCompanionBuilder,
          $$DraftEntriesTableUpdateCompanionBuilder,
          (
            DraftEntry,
            BaseReferences<_$AppDatabase, $DraftEntriesTable, DraftEntry>,
          ),
          DraftEntry,
          PrefetchHooks Function()
        > {
  $$DraftEntriesTableTableManager(_$AppDatabase db, $DraftEntriesTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$DraftEntriesTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$DraftEntriesTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$DraftEntriesTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> id = const Value.absent(),
                Value<String> type = const Value.absent(),
                Value<String> payloadJson = const Value.absent(),
                Value<DateTime> updatedAt = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => DraftEntriesCompanion(
                id: id,
                type: type,
                payloadJson: payloadJson,
                updatedAt: updatedAt,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String id,
                required String type,
                required String payloadJson,
                required DateTime updatedAt,
                Value<int> rowid = const Value.absent(),
              }) => DraftEntriesCompanion.insert(
                id: id,
                type: type,
                payloadJson: payloadJson,
                updatedAt: updatedAt,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$DraftEntriesTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $DraftEntriesTable,
      DraftEntry,
      $$DraftEntriesTableFilterComposer,
      $$DraftEntriesTableOrderingComposer,
      $$DraftEntriesTableAnnotationComposer,
      $$DraftEntriesTableCreateCompanionBuilder,
      $$DraftEntriesTableUpdateCompanionBuilder,
      (
        DraftEntry,
        BaseReferences<_$AppDatabase, $DraftEntriesTable, DraftEntry>,
      ),
      DraftEntry,
      PrefetchHooks Function()
    >;

class $AppDatabaseManager {
  final _$AppDatabase _db;
  $AppDatabaseManager(this._db);
  $$CachedJobsTableTableManager get cachedJobs =>
      $$CachedJobsTableTableManager(_db, _db.cachedJobs);
  $$CachedScheduleEntriesTableTableManager get cachedScheduleEntries =>
      $$CachedScheduleEntriesTableTableManager(_db, _db.cachedScheduleEntries);
  $$CachedMapAssetsTableTableManager get cachedMapAssets =>
      $$CachedMapAssetsTableTableManager(_db, _db.cachedMapAssets);
  $$CachedMapAssetSyncCursorsTableTableManager get cachedMapAssetSyncCursors =>
      $$CachedMapAssetSyncCursorsTableTableManager(
        _db,
        _db.cachedMapAssetSyncCursors,
      );
  $$OutboxEntriesTableTableManager get outboxEntries =>
      $$OutboxEntriesTableTableManager(_db, _db.outboxEntries);
  $$PendingPhotosTableTableManager get pendingPhotos =>
      $$PendingPhotosTableTableManager(_db, _db.pendingPhotos);
  $$DraftEntriesTableTableManager get draftEntries =>
      $$DraftEntriesTableTableManager(_db, _db.draftEntries);
}
