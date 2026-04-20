--
-- PostgreSQL database dump
--

-- Dumped from database version 16.4 (Debian 16.4-1.pgdg110+2)
-- Dumped by pg_dump version 16.4 (Debian 16.4-1.pgdg110+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: topology; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA topology;


--
-- Name: SCHEMA topology; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA topology IS 'PostGIS Topology schema';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: postgis; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;


--
-- Name: EXTENSION postgis; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION postgis IS 'PostGIS geometry and geography spatial types and functions';


--
-- Name: postgis_topology; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS postgis_topology WITH SCHEMA topology;


--
-- Name: EXTENSION postgis_topology; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION postgis_topology IS 'PostGIS topology spatial types and functions';


--
-- Name: accesstype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.accesstype AS ENUM (
    'fiber',
    'fixed_wireless',
    'dsl',
    'cable'
);


--
-- Name: accountingstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.accountingstatus AS ENUM (
    'start',
    'interim',
    'stop'
);


--
-- Name: addontype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.addontype AS ENUM (
    'static_ip',
    'router_rental',
    'install_fee',
    'premium_support',
    'extra_ip',
    'managed_wifi',
    'custom'
);


--
-- Name: addresstype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.addresstype AS ENUM (
    'service',
    'billing',
    'mailing'
);


--
-- Name: alertoperator; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.alertoperator AS ENUM (
    'gt',
    'gte',
    'lt',
    'lte',
    'eq'
);


--
-- Name: alertseverity; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.alertseverity AS ENUM (
    'info',
    'warning',
    'critical'
);


--
-- Name: alertstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.alertstatus AS ENUM (
    'open',
    'acknowledged',
    'resolved'
);


--
-- Name: appointmentstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.appointmentstatus AS ENUM (
    'proposed',
    'confirmed',
    'completed',
    'no_show',
    'canceled'
);


--
-- Name: arrangementstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.arrangementstatus AS ENUM (
    'pending',
    'active',
    'completed',
    'defaulted',
    'canceled'
);


--
-- Name: auditactortype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.auditactortype AS ENUM (
    'system',
    'user',
    'api_key',
    'service'
);


--
-- Name: authprovider; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.authprovider AS ENUM (
    'local',
    'sso',
    'radius'
);


--
-- Name: bankaccounttype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.bankaccounttype AS ENUM (
    'checking',
    'savings',
    'business',
    'other'
);


--
-- Name: billingcycle; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.billingcycle AS ENUM (
    'daily',
    'weekly',
    'monthly',
    'quarterly',
    'annual'
);


--
-- Name: billingmode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.billingmode AS ENUM (
    'prepaid',
    'postpaid'
);


--
-- Name: billingrunstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.billingrunstatus AS ENUM (
    'running',
    'success',
    'failed'
);


--
-- Name: buildoutmilestonestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.buildoutmilestonestatus AS ENUM (
    'pending',
    'in_progress',
    'completed',
    'blocked',
    'canceled'
);


--
-- Name: buildoutprojectstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.buildoutprojectstatus AS ENUM (
    'planned',
    'in_progress',
    'blocked',
    'ready',
    'completed',
    'canceled'
);


--
-- Name: buildoutrequeststatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.buildoutrequeststatus AS ENUM (
    'submitted',
    'approved',
    'rejected',
    'canceled'
);


--
-- Name: buildoutstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.buildoutstatus AS ENUM (
    'planned',
    'in_progress',
    'ready',
    'not_planned'
);


--
-- Name: channeltype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.channeltype AS ENUM (
    'email',
    'phone',
    'sms',
    'whatsapp'
);


--
-- Name: collectionaccounttype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.collectionaccounttype AS ENUM (
    'bank',
    'cash',
    'other'
);


--
-- Name: communicationchannel; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.communicationchannel AS ENUM (
    'email',
    'sms',
    'in_app',
    'whatsapp'
);


--
-- Name: communicationdirection; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.communicationdirection AS ENUM (
    'inbound',
    'outbound'
);


--
-- Name: communicationstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.communicationstatus AS ENUM (
    'pending',
    'sent',
    'delivered',
    'failed',
    'bounced'
);


--
-- Name: configbackupmethod; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.configbackupmethod AS ENUM (
    'ssh',
    'api',
    'tftp',
    'ftp',
    'snmp'
);


--
-- Name: configmethod; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.configmethod AS ENUM (
    'omci',
    'tr069'
);


--
-- Name: connectiontype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.connectiontype AS ENUM (
    'pppoe',
    'dhcp',
    'ipoe',
    'static',
    'hotspot'
);


--
-- Name: connectorauthtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.connectorauthtype AS ENUM (
    'none',
    'basic',
    'bearer',
    'hmac',
    'api_key',
    'oauth2'
);


--
-- Name: connectortype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.connectortype AS ENUM (
    'webhook',
    'http',
    'email',
    'whatsapp',
    'smtp',
    'stripe',
    'twilio',
    'facebook',
    'instagram',
    'custom'
);


--
-- Name: contactmethod; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.contactmethod AS ENUM (
    'email',
    'phone',
    'sms',
    'push'
);


--
-- Name: contractterm; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.contractterm AS ENUM (
    'month_to_month',
    'twelve_month',
    'twentyfour_month'
);


--
-- Name: cpe_devicestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.cpe_devicestatus AS ENUM (
    'active',
    'inactive',
    'maintenance',
    'retired'
);


--
-- Name: creditnotestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.creditnotestatus AS ENUM (
    'draft',
    'issued',
    'partially_applied',
    'applied',
    'void'
);


--
-- Name: customernotificationstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.customernotificationstatus AS ENUM (
    'pending',
    'sent',
    'failed'
);


--
-- Name: deliverystatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.deliverystatus AS ENUM (
    'accepted',
    'delivered',
    'failed',
    'bounced',
    'rejected'
);


--
-- Name: devicerole; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.devicerole AS ENUM (
    'core',
    'distribution',
    'access',
    'aggregation',
    'edge',
    'cpe'
);


--
-- Name: devicestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.devicestatus AS ENUM (
    'active',
    'inactive',
    'maintenance',
    'retired'
);


--
-- Name: devicetype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.devicetype AS ENUM (
    'ont',
    'router',
    'switch',
    'hub',
    'firewall',
    'inverter',
    'access_point',
    'bridge',
    'modem',
    'server',
    'cpe',
    'other'
);


--
-- Name: discounttype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.discounttype AS ENUM (
    'percentage',
    'percent',
    'fixed'
);


--
-- Name: dnsthreataction; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.dnsthreataction AS ENUM (
    'blocked',
    'allowed',
    'monitored'
);


--
-- Name: dnsthreatseverity; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.dnsthreatseverity AS ENUM (
    'low',
    'medium',
    'high',
    'critical'
);


--
-- Name: dunningaction; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.dunningaction AS ENUM (
    'notify',
    'throttle',
    'suspend',
    'reject'
);


--
-- Name: dunningcasestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.dunningcasestatus AS ENUM (
    'open',
    'paused',
    'resolved',
    'closed'
);


--
-- Name: enforcementreason; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.enforcementreason AS ENUM (
    'overdue',
    'fup',
    'prepaid',
    'admin',
    'customer_hold',
    'fraud',
    'system'
);


--
-- Name: eventstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.eventstatus AS ENUM (
    'pending',
    'processing',
    'completed',
    'failed'
);


--
-- Name: executionmethod; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.executionmethod AS ENUM (
    'ssh',
    'api',
    'radius_coa'
);


--
-- Name: externalentitytype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.externalentitytype AS ENUM (
    'subscriber',
    'subscription',
    'invoice',
    'service_order',
    'ticket'
);


--
-- Name: fibercabletype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fibercabletype AS ENUM (
    'single_mode',
    'multi_mode',
    'armored',
    'aerial',
    'underground',
    'direct_buried'
);


--
-- Name: fiberchangerequestoperation; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fiberchangerequestoperation AS ENUM (
    'create',
    'update',
    'delete'
);


--
-- Name: fiberchangerequeststatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fiberchangerequeststatus AS ENUM (
    'pending',
    'applied',
    'rejected'
);


--
-- Name: fiberendpointtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fiberendpointtype AS ENUM (
    'olt_port',
    'splitter_port',
    'fdh',
    'ont',
    'splice_closure',
    'other'
);


--
-- Name: fibersegmenttype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fibersegmenttype AS ENUM (
    'feeder',
    'distribution',
    'drop'
);


--
-- Name: fiberstrandstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fiberstrandstatus AS ENUM (
    'available',
    'in_use',
    'reserved',
    'damaged',
    'retired'
);


--
-- Name: fupaction; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fupaction AS ENUM (
    'reduce_speed',
    'block',
    'notify'
);


--
-- Name: fupactionstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fupactionstatus AS ENUM (
    'none',
    'throttled',
    'blocked',
    'notified'
);


--
-- Name: fupconsumptionperiod; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fupconsumptionperiod AS ENUM (
    'monthly',
    'daily',
    'weekly'
);


--
-- Name: fupdataunit; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fupdataunit AS ENUM (
    'mb',
    'gb',
    'tb'
);


--
-- Name: fupdirection; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fupdirection AS ENUM (
    'up',
    'down',
    'up_down'
);


--
-- Name: gender; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.gender AS ENUM (
    'unknown',
    'female',
    'male',
    'non_binary',
    'other'
);


--
-- Name: geoareatype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.geoareatype AS ENUM (
    'coverage',
    'service_area',
    'region',
    'custom'
);


--
-- Name: geolayersource; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.geolayersource AS ENUM (
    'locations',
    'areas'
);


--
-- Name: geolayertype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.geolayertype AS ENUM (
    'points',
    'lines',
    'polygons',
    'heatmap',
    'cluster'
);


--
-- Name: geolocationtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.geolocationtype AS ENUM (
    'address',
    'pop',
    'site',
    'customer',
    'asset',
    'custom'
);


--
-- Name: gponchannel; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.gponchannel AS ENUM (
    'gpon',
    'xg_pon',
    'xgs_pon'
);


--
-- Name: guaranteedspeedtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.guaranteedspeedtype AS ENUM (
    'none',
    'relative',
    'fixed'
);


--
-- Name: hardwareunitstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.hardwareunitstatus AS ENUM (
    'active',
    'inactive',
    'failed',
    'unknown'
);


--
-- Name: healthstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.healthstatus AS ENUM (
    'unknown',
    'healthy',
    'degraded',
    'unhealthy'
);


--
-- Name: installmentstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.installmentstatus AS ENUM (
    'pending',
    'due',
    'paid',
    'overdue',
    'waived'
);


--
-- Name: integrationconnectorstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationconnectorstatus AS ENUM (
    'enabled',
    'disabled',
    'not_installed'
);


--
-- Name: integrationconnectortype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationconnectortype AS ENUM (
    'payment',
    'accounting',
    'messaging',
    'network',
    'crm',
    'voice',
    'custom'
);


--
-- Name: integrationhookauthtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationhookauthtype AS ENUM (
    'none',
    'bearer',
    'basic',
    'hmac'
);


--
-- Name: integrationhookexecutionstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationhookexecutionstatus AS ENUM (
    'success',
    'failed'
);


--
-- Name: integrationhooktype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationhooktype AS ENUM (
    'web',
    'cli',
    'internal'
);


--
-- Name: integrationjobtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationjobtype AS ENUM (
    'sync',
    'export',
    'import_'
);


--
-- Name: integrationrunstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationrunstatus AS ENUM (
    'running',
    'success',
    'failed'
);


--
-- Name: integrationscheduletype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationscheduletype AS ENUM (
    'manual',
    'interval'
);


--
-- Name: integrationtargettype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.integrationtargettype AS ENUM (
    'radius',
    'crm',
    'billing',
    'n8n',
    'custom'
);


--
-- Name: interfacestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.interfacestatus AS ENUM (
    'up',
    'down',
    'unknown'
);


--
-- Name: invoicepdfexportstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.invoicepdfexportstatus AS ENUM (
    'queued',
    'processing',
    'completed',
    'failed'
);


--
-- Name: invoicestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.invoicestatus AS ENUM (
    'draft',
    'issued',
    'partially_paid',
    'paid',
    'void',
    'overdue'
);


--
-- Name: ipprotocol; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ipprotocol AS ENUM (
    'ipv4',
    'dual_stack'
);


--
-- Name: ipversion; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ipversion AS ENUM (
    'ipv4',
    'ipv6'
);


--
-- Name: ledgercategory; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ledgercategory AS ENUM (
    'internet_service',
    'custom_service',
    'voice_service',
    'bundle_service',
    'installation_fee',
    'equipment_rental',
    'equipment_purchase',
    'late_payment_fee',
    'reconnection_fee',
    'deposit',
    'discount',
    'tax',
    'overage',
    'top_up',
    'other'
);


--
-- Name: ledgerentrytype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ledgerentrytype AS ENUM (
    'debit',
    'credit'
);


--
-- Name: ledgersource; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ledgersource AS ENUM (
    'invoice',
    'payment',
    'adjustment',
    'refund',
    'credit_note',
    'other'
);


--
-- Name: legaldocumenttype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.legaldocumenttype AS ENUM (
    'terms_of_service',
    'privacy_policy',
    'acceptable_use',
    'service_level_agreement',
    'data_processing',
    'cookie_policy',
    'refund_policy',
    'other'
);


--
-- Name: lifecycleeventtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.lifecycleeventtype AS ENUM (
    'activate',
    'suspend',
    'resume',
    'cancel',
    'upgrade',
    'downgrade',
    'change_address',
    'other'
);


--
-- Name: metrictype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.metrictype AS ENUM (
    'cpu',
    'memory',
    'temperature',
    'rx_bps',
    'tx_bps',
    'uptime',
    'custom'
);


--
-- Name: mfamethodtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.mfamethodtype AS ENUM (
    'totp',
    'sms',
    'email'
);


--
-- Name: mgmtipmode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.mgmtipmode AS ENUM (
    'inactive',
    'static_ip',
    'dhcp'
);


--
-- Name: monitoring_devicestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.monitoring_devicestatus AS ENUM (
    'online',
    'offline',
    'degraded',
    'maintenance'
);


--
-- Name: nasdevicestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.nasdevicestatus AS ENUM (
    'active',
    'maintenance',
    'offline',
    'decommissioned'
);


--
-- Name: nasvendor; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.nasvendor AS ENUM (
    'mikrotik',
    'huawei',
    'ubiquiti',
    'cisco',
    'juniper',
    'cambium',
    'nokia',
    'zte',
    'other'
);


--
-- Name: networkoperationstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.networkoperationstatus AS ENUM (
    'pending',
    'running',
    'waiting',
    'succeeded',
    'failed',
    'canceled'
);


--
-- Name: networkoperationtargettype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.networkoperationtargettype AS ENUM (
    'olt',
    'ont',
    'cpe'
);


--
-- Name: networkoperationtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.networkoperationtype AS ENUM (
    'olt_ont_sync',
    'olt_pon_repair',
    'ont_provision',
    'ont_authorize',
    'ont_reboot',
    'ont_factory_reset',
    'ont_set_pppoe',
    'ont_set_conn_request_creds',
    'ont_send_conn_request',
    'ont_enable_ipv6',
    'cpe_set_conn_request_creds',
    'cpe_send_conn_request',
    'cpe_reboot',
    'cpe_factory_reset',
    'tr069_bootstrap',
    'wifi_update',
    'pppoe_push',
    'router_config_push',
    'router_config_backup',
    'router_reboot',
    'router_firmware_upgrade',
    'router_bulk_push'
);


--
-- Name: notificationchannel; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.notificationchannel AS ENUM (
    'email',
    'sms',
    'push',
    'whatsapp',
    'webhook'
);


--
-- Name: notificationstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.notificationstatus AS ENUM (
    'queued',
    'sending',
    'delivered',
    'failed',
    'canceled'
);


--
-- Name: odnendpointtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.odnendpointtype AS ENUM (
    'fdh',
    'splitter',
    'splitter_port',
    'pon_port',
    'olt_port',
    'ont',
    'terminal',
    'splice_closure',
    'other'
);


--
-- Name: offerstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.offerstatus AS ENUM (
    'active',
    'inactive',
    'archived'
);


--
-- Name: oltconfigbackuptype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.oltconfigbackuptype AS ENUM (
    'auto',
    'manual'
);


--
-- Name: oltporttype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.oltporttype AS ENUM (
    'pon',
    'uplink',
    'ethernet',
    'mgmt'
);


--
-- Name: ontprofiletype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ontprofiletype AS ENUM (
    'residential',
    'business',
    'management'
);


--
-- Name: ontprovisioningstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ontprovisioningstatus AS ENUM (
    'unprovisioned',
    'provisioned',
    'drift_detected',
    'failed'
);


--
-- Name: onucapability; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.onucapability AS ENUM (
    'bridging',
    'routing',
    'bridging_routing'
);


--
-- Name: onumode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.onumode AS ENUM (
    'routing',
    'bridging'
);


--
-- Name: onuofflinereason; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.onuofflinereason AS ENUM (
    'power_fail',
    'los',
    'dying_gasp',
    'unknown'
);


--
-- Name: onuonlinestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.onuonlinestatus AS ENUM (
    'online',
    'offline',
    'unknown'
);


--
-- Name: paymentchanneltype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.paymentchanneltype AS ENUM (
    'card',
    'bank_transfer',
    'cash',
    'check',
    'transfer',
    'other'
);


--
-- Name: paymentfrequency; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.paymentfrequency AS ENUM (
    'weekly',
    'biweekly',
    'monthly'
);


--
-- Name: paymentmethodtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.paymentmethodtype AS ENUM (
    'card',
    'bank_account',
    'cash',
    'check',
    'transfer',
    'other'
);


--
-- Name: paymentprovidereventstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.paymentprovidereventstatus AS ENUM (
    'pending',
    'processed',
    'failed'
);


--
-- Name: paymentprovidertype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.paymentprovidertype AS ENUM (
    'stripe',
    'paypal',
    'paystack',
    'flutterwave',
    'manual',
    'custom'
);


--
-- Name: paymentstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.paymentstatus AS ENUM (
    'pending',
    'succeeded',
    'failed',
    'refunded',
    'partially_refunded',
    'canceled'
);


--
-- Name: plancategory; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.plancategory AS ENUM (
    'internet',
    'recurring',
    'one_time',
    'bundle'
);


--
-- Name: pontype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.pontype AS ENUM (
    'gpon',
    'epon'
);


--
-- Name: portalmessagestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.portalmessagestatus AS ENUM (
    'unread',
    'read',
    'archived'
);


--
-- Name: portalmessagetype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.portalmessagetype AS ENUM (
    'welcome',
    'announcement',
    'billing',
    'service',
    'support',
    'system'
);


--
-- Name: portstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.portstatus AS ENUM (
    'up',
    'down',
    'disabled'
);


--
-- Name: porttype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.porttype AS ENUM (
    'pon',
    'ethernet',
    'wifi',
    'mgmt'
);


--
-- Name: pppoepasswordmode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.pppoepasswordmode AS ENUM (
    'from_credential',
    'generate',
    'static'
);


--
-- Name: pricebasis; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.pricebasis AS ENUM (
    'flat',
    'usage',
    'tiered',
    'hybrid'
);


--
-- Name: pricetype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.pricetype AS ENUM (
    'recurring',
    'one_time',
    'usage'
);


--
-- Name: priceunit; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.priceunit AS ENUM (
    'day',
    'week',
    'month',
    'year',
    'gb',
    'tb',
    'item'
);


--
-- Name: prorationpolicy; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.prorationpolicy AS ENUM (
    'immediate',
    'next_cycle',
    'none'
);


--
-- Name: provisioning_taskstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.provisioning_taskstatus AS ENUM (
    'pending',
    'in_progress',
    'blocked',
    'completed',
    'failed'
);


--
-- Name: provisioningaction; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.provisioningaction AS ENUM (
    'create_user',
    'delete_user',
    'suspend_user',
    'unsuspend_user',
    'change_speed',
    'change_ip',
    'reset_session',
    'get_user_info',
    'backup_config',
    'restore_config'
);


--
-- Name: provisioninglogstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.provisioninglogstatus AS ENUM (
    'pending',
    'running',
    'success',
    'failed',
    'timeout'
);


--
-- Name: provisioningrunstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.provisioningrunstatus AS ENUM (
    'pending',
    'running',
    'success',
    'failed'
);


--
-- Name: provisioningsteptype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.provisioningsteptype AS ENUM (
    'assign_ont',
    'push_config',
    'confirm_up',
    'resolve_profile',
    'push_ont_profile',
    'verify_ont_config',
    'create_olt_service_port',
    'ensure_nas_vlan',
    'push_tr069_wan_config',
    'push_tr069_pppoe_credentials'
);


--
-- Name: provisioningvendor; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.provisioningvendor AS ENUM (
    'mikrotik',
    'huawei',
    'zte',
    'nokia',
    'genieacs',
    'other'
);


--
-- Name: qualificationstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.qualificationstatus AS ENUM (
    'eligible',
    'ineligible',
    'needs_buildout'
);


--
-- Name: radiusautherrortype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.radiusautherrortype AS ENUM (
    'reject',
    'timeout',
    'invalid_credentials',
    'disabled_account',
    'expired_account',
    'nas_mismatch',
    'policy_violation',
    'other'
);


--
-- Name: radiussyncstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.radiussyncstatus AS ENUM (
    'running',
    'success',
    'failed'
);


--
-- Name: refundpolicy; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.refundpolicy AS ENUM (
    'none',
    'prorated',
    'full_within_days'
);


--
-- Name: routeraccessmethod; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.routeraccessmethod AS ENUM (
    'direct',
    'jump_host'
);


--
-- Name: routerconfigpushstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.routerconfigpushstatus AS ENUM (
    'pending',
    'running',
    'completed',
    'partial_failure',
    'failed',
    'rolled_back'
);


--
-- Name: routerpushresultstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.routerpushresultstatus AS ENUM (
    'pending',
    'success',
    'failed',
    'skipped'
);


--
-- Name: routersnapshotsource; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.routersnapshotsource AS ENUM (
    'manual',
    'scheduled',
    'pre_change',
    'post_change'
);


--
-- Name: routerstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.routerstatus AS ENUM (
    'online',
    'offline',
    'degraded',
    'maintenance',
    'unreachable'
);


--
-- Name: routertemplatecategory; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.routertemplatecategory AS ENUM (
    'firewall',
    'queue',
    'address_list',
    'routing',
    'dns',
    'ntp',
    'snmp',
    'system',
    'custom'
);


--
-- Name: scheduletype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.scheduletype AS ENUM (
    'interval'
);


--
-- Name: serviceorderstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.serviceorderstatus AS ENUM (
    'draft',
    'submitted',
    'scheduled',
    'provisioning',
    'active',
    'canceled',
    'failed'
);


--
-- Name: serviceordertype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.serviceordertype AS ENUM (
    'new_install',
    'upgrade',
    'downgrade',
    'disconnect',
    'reconnect',
    'change_service'
);


--
-- Name: servicestate; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.servicestate AS ENUM (
    'pending',
    'installing',
    'provisioning',
    'active',
    'suspended',
    'canceled',
    'disconnected'
);


--
-- Name: servicetype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.servicetype AS ENUM (
    'residential',
    'business'
);


--
-- Name: sessionstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.sessionstatus AS ENUM (
    'active',
    'revoked',
    'expired'
);


--
-- Name: settingdomain; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.settingdomain AS ENUM (
    'auth',
    'audit',
    'billing',
    'catalog',
    'subscriber',
    'imports',
    'notification',
    'network',
    'network_monitoring',
    'provisioning',
    'geocoding',
    'usage',
    'radius',
    'collections',
    'lifecycle',
    'projects',
    'workflow',
    'modules',
    'inventory',
    'comms',
    'tr069',
    'snmp',
    'bandwidth',
    'subscription_engine',
    'gis',
    'scheduler'
);


--
-- Name: settingvaluetype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.settingvaluetype AS ENUM (
    'string',
    'integer',
    'boolean',
    'json'
);


--
-- Name: snmpauthprotocol; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.snmpauthprotocol AS ENUM (
    'none',
    'md5',
    'sha'
);


--
-- Name: snmpprivprotocol; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.snmpprivprotocol AS ENUM (
    'none',
    'des',
    'aes'
);


--
-- Name: snmpversion; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.snmpversion AS ENUM (
    'v2c',
    'v3'
);


--
-- Name: speedprofiledirection; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.speedprofiledirection AS ENUM (
    'download',
    'upload'
);


--
-- Name: speedprofiletype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.speedprofiletype AS ENUM (
    'internet',
    'management'
);


--
-- Name: speedtestsource; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.speedtestsource AS ENUM (
    'manual',
    'scheduled',
    'api'
);


--
-- Name: splitterporttype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.splitterporttype AS ENUM (
    'input',
    'output'
);


--
-- Name: splynxentitytype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.splynxentitytype AS ENUM (
    'customer',
    'service',
    'tariff',
    'invoice',
    'payment',
    'transaction',
    'credit_note',
    'ticket',
    'quote',
    'router',
    'location',
    'partner',
    'email',
    'sms',
    'scheduling_task',
    'inventory_item'
);


--
-- Name: subscribercategory; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.subscribercategory AS ENUM (
    'residential',
    'business',
    'government',
    'ngo'
);


--
-- Name: subscriberstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.subscriberstatus AS ENUM (
    'new',
    'active',
    'blocked',
    'suspended',
    'disabled',
    'canceled',
    'delinquent'
);


--
-- Name: subscriptionchangestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.subscriptionchangestatus AS ENUM (
    'pending',
    'approved',
    'rejected',
    'applied',
    'canceled'
);


--
-- Name: subscriptionstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.subscriptionstatus AS ENUM (
    'pending',
    'active',
    'blocked',
    'suspended',
    'stopped',
    'disabled',
    'hidden',
    'archived',
    'canceled',
    'expired'
);


--
-- Name: suspensionaction; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.suspensionaction AS ENUM (
    'none',
    'throttle',
    'suspend',
    'reject'
);


--
-- Name: taxapplication; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.taxapplication AS ENUM (
    'exclusive',
    'inclusive',
    'exempt'
);


--
-- Name: ticketchannel; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ticketchannel AS ENUM (
    'web',
    'email',
    'phone',
    'chat',
    'api'
);


--
-- Name: ticketpriority; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ticketpriority AS ENUM (
    'lower',
    'low',
    'medium',
    'normal',
    'high',
    'urgent'
);


--
-- Name: ticketstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ticketstatus AS ENUM (
    'new',
    'open',
    'pending',
    'waiting_on_customer',
    'lastmile_rerun',
    'site_under_construction',
    'on_hold',
    'resolved',
    'closed',
    'canceled',
    'merged'
);


--
-- Name: topologylinkadminstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.topologylinkadminstatus AS ENUM (
    'enabled',
    'disabled',
    'maintenance'
);


--
-- Name: topologylinkmedium; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.topologylinkmedium AS ENUM (
    'fiber',
    'wireless',
    'ethernet',
    'virtual',
    'unknown'
);


--
-- Name: topologylinkrole; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.topologylinkrole AS ENUM (
    'uplink',
    'backhaul',
    'peering',
    'lag_member',
    'crossconnect',
    'access',
    'distribution',
    'core',
    'unknown'
);


--
-- Name: tr069event; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.tr069event AS ENUM (
    'boot',
    'bootstrap',
    'periodic',
    'value_change',
    'connection_request',
    'transfer_complete',
    'diagnostics_complete'
);


--
-- Name: tr069jobstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.tr069jobstatus AS ENUM (
    'queued',
    'running',
    'pending',
    'succeeded',
    'failed',
    'canceled'
);


--
-- Name: usagechargestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.usagechargestatus AS ENUM (
    'staged',
    'posted',
    'needs_review',
    'skipped'
);


--
-- Name: usageratingrunstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.usageratingrunstatus AS ENUM (
    'running',
    'success',
    'failed'
);


--
-- Name: usagesource; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.usagesource AS ENUM (
    'radius',
    'dhcp',
    'snmp',
    'api'
);


--
-- Name: usertype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.usertype AS ENUM (
    'system_user',
    'customer',
    'reseller'
);


--
-- Name: vlanmode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.vlanmode AS ENUM (
    'tagged',
    'untagged',
    'transparent',
    'translate'
);


--
-- Name: vlanpurpose; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.vlanpurpose AS ENUM (
    'internet',
    'management',
    'tr069',
    'iptv',
    'voip',
    'other'
);


--
-- Name: wanconnectiontype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.wanconnectiontype AS ENUM (
    'pppoe',
    'dhcp',
    'static',
    'bridged'
);


--
-- Name: wanmode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.wanmode AS ENUM (
    'dhcp',
    'static_ip',
    'pppoe',
    'setup_via_onu'
);


--
-- Name: wanservicetype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.wanservicetype AS ENUM (
    'internet',
    'iptv',
    'voip',
    'management',
    'data'
);


--
-- Name: webhookdeliverystatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.webhookdeliverystatus AS ENUM (
    'pending',
    'delivered',
    'failed'
);


--
-- Name: webhookeventtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.webhookeventtype AS ENUM (
    'subscriber_created',
    'subscriber_updated',
    'subscriber_suspended',
    'subscriber_reactivated',
    'subscription_created',
    'subscription_activated',
    'subscription_suspended',
    'subscription_resumed',
    'subscription_canceled',
    'subscription_upgraded',
    'subscription_downgraded',
    'subscription_expiring',
    'invoice_created',
    'invoice_sent',
    'invoice_paid',
    'invoice_overdue',
    'payment_received',
    'payment_failed',
    'payment_refunded',
    'usage_recorded',
    'usage_warning',
    'usage_exhausted',
    'usage_topped_up',
    'provisioning_started',
    'provisioning_completed',
    'provisioning_failed',
    'service_order_created',
    'service_order_assigned',
    'service_order_completed',
    'appointment_scheduled',
    'appointment_missed',
    'device_offline',
    'device_online',
    'session_started',
    'session_ended',
    'network_alert',
    'custom'
);


--
-- Name: wireguardpeerstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.wireguardpeerstatus AS ENUM (
    'active',
    'disabled'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: access_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.access_credentials (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    username character varying(120) NOT NULL,
    secret_hash character varying(255),
    is_active boolean NOT NULL,
    last_auth_at timestamp with time zone,
    radius_profile_id uuid,
    circuit_id character varying(255),
    remote_id character varying(255),
    connection_type public.connectiontype,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: add_on_prices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.add_on_prices (
    id uuid NOT NULL,
    add_on_id uuid NOT NULL,
    price_type public.pricetype NOT NULL,
    amount numeric(10,2) NOT NULL,
    currency character varying(3) NOT NULL,
    billing_cycle public.billingcycle,
    unit public.priceunit,
    description character varying(200),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: add_ons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.add_ons (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    addon_type public.addontype NOT NULL,
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: addresses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.addresses (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    tax_rate_id uuid,
    address_type public.addresstype NOT NULL,
    label character varying(120),
    address_line1 character varying(120) NOT NULL,
    address_line2 character varying(120),
    city character varying(80),
    region character varying(80),
    postal_code character varying(20),
    country_code character varying(2),
    latitude double precision,
    longitude double precision,
    geom public.geometry(Point,4326),
    is_primary boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: alert_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_events (
    id uuid NOT NULL,
    alert_id uuid NOT NULL,
    status public.alertstatus NOT NULL,
    message character varying(255),
    created_at timestamp with time zone NOT NULL
);


--
-- Name: alert_notification_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_notification_logs (
    id uuid NOT NULL,
    alert_id uuid NOT NULL,
    policy_id uuid NOT NULL,
    notification_id uuid,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: alert_notification_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_notification_policies (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    channel public.notificationchannel NOT NULL,
    recipient character varying(255) NOT NULL,
    template_id uuid,
    rule_id uuid,
    device_id uuid,
    interface_id uuid,
    severity_min public.alertseverity NOT NULL,
    status public.alertstatus NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: alert_notification_policy_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_notification_policy_steps (
    id uuid NOT NULL,
    policy_id uuid NOT NULL,
    step_index integer NOT NULL,
    delay_minutes integer NOT NULL,
    channel public.notificationchannel NOT NULL,
    recipient character varying(255),
    template_id uuid,
    connector_config_id uuid,
    rotation_id uuid,
    severity_min public.alertseverity NOT NULL,
    status public.alertstatus NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: alert_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_rules (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    metric_type public.metrictype NOT NULL,
    operator public.alertoperator NOT NULL,
    threshold double precision NOT NULL,
    duration_seconds integer,
    severity public.alertseverity NOT NULL,
    device_id uuid,
    interface_id uuid,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alerts (
    id uuid NOT NULL,
    rule_id uuid NOT NULL,
    device_id uuid,
    interface_id uuid,
    metric_type public.metrictype NOT NULL,
    measured_value double precision NOT NULL,
    status public.alertstatus NOT NULL,
    severity public.alertseverity NOT NULL,
    triggered_at timestamp with time zone NOT NULL,
    acknowledged_at timestamp with time zone,
    resolved_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_keys (
    id uuid NOT NULL,
    subscriber_id uuid,
    system_user_id uuid,
    label character varying(120),
    key_hash character varying(255) NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone
);


--
-- Name: audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_events (
    id uuid NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    actor_type public.auditactortype NOT NULL,
    actor_id character varying(120),
    action character varying(80) NOT NULL,
    entity_type character varying(160) NOT NULL,
    entity_id character varying(120),
    status_code integer,
    is_success boolean NOT NULL,
    is_active boolean NOT NULL,
    ip_address character varying(64),
    user_agent character varying(255),
    request_id character varying(120),
    metadata json
);


--
-- Name: bandwidth_samples; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bandwidth_samples (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    device_id uuid,
    interface_id uuid,
    rx_bps integer NOT NULL,
    tx_bps integer NOT NULL,
    sample_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: bank_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_accounts (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    payment_method_id uuid,
    bank_name character varying(120),
    account_type public.bankaccounttype NOT NULL,
    account_last4 character varying(4),
    routing_last4 character varying(4),
    token character varying(255),
    is_default boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: bank_reconciliation_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_items (
    id uuid NOT NULL,
    run_id uuid NOT NULL,
    item_type character varying(20) NOT NULL,
    reference character varying(255),
    file_name character varying(255),
    count integer NOT NULL,
    amount numeric(14,2) NOT NULL,
    metadata json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: bank_reconciliation_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_runs (
    id uuid NOT NULL,
    date_range character varying(20),
    handler character varying(120),
    statement_rows integer NOT NULL,
    imported_rows integer NOT NULL,
    unmatched_rows integer NOT NULL,
    system_payment_count integer NOT NULL,
    statement_total numeric(14,2) NOT NULL,
    payment_total numeric(14,2) NOT NULL,
    difference_total numeric(14,2) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: billing_run_schedules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.billing_run_schedules (
    id uuid NOT NULL,
    enabled boolean NOT NULL,
    run_day integer NOT NULL,
    run_time character varying(8) NOT NULL,
    timezone character varying(64) NOT NULL,
    billing_cycle character varying(40) NOT NULL,
    partner_ids json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: billing_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.billing_runs (
    id uuid NOT NULL,
    run_at timestamp with time zone NOT NULL,
    billing_cycle character varying(40),
    status public.billingrunstatus NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    subscriptions_scanned integer NOT NULL,
    subscriptions_billed integer NOT NULL,
    invoices_created integer NOT NULL,
    lines_created integer NOT NULL,
    skipped integer NOT NULL,
    error text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: buildout_milestones; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.buildout_milestones (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    status public.buildoutmilestonestatus NOT NULL,
    order_index integer NOT NULL,
    due_at timestamp with time zone,
    completed_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: buildout_projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.buildout_projects (
    id uuid NOT NULL,
    request_id uuid,
    coverage_area_id uuid,
    address_id uuid,
    status public.buildoutprojectstatus NOT NULL,
    progress_percent integer NOT NULL,
    target_ready_date timestamp with time zone,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: buildout_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.buildout_requests (
    id uuid NOT NULL,
    qualification_id uuid,
    coverage_area_id uuid,
    address_id uuid,
    requested_by character varying(120),
    status public.buildoutrequeststatus NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: buildout_updates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.buildout_updates (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    status public.buildoutprojectstatus NOT NULL,
    message text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: catalog_offers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.catalog_offers (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(60),
    service_type public.servicetype NOT NULL,
    access_type public.accesstype NOT NULL,
    price_basis public.pricebasis NOT NULL,
    billing_cycle public.billingcycle NOT NULL,
    billing_mode public.billingmode NOT NULL,
    contract_term public.contractterm NOT NULL,
    region_zone_id uuid,
    usage_allowance_id uuid,
    sla_profile_id uuid,
    policy_set_id uuid,
    splynx_tariff_id integer,
    splynx_service_name character varying(160),
    splynx_tax_id integer,
    with_vat boolean NOT NULL,
    vat_percent numeric(5,2),
    speed_download_mbps integer,
    speed_upload_mbps integer,
    guaranteed_speed_limit_at integer,
    guaranteed_speed public.guaranteedspeedtype NOT NULL,
    aggregation integer,
    priority character varying(40),
    available_for_services boolean NOT NULL,
    show_on_customer_portal boolean NOT NULL,
    plan_category public.plancategory DEFAULT 'internet'::public.plancategory NOT NULL,
    hide_on_admin_portal boolean NOT NULL,
    service_description text,
    burst_profile character varying(120),
    prepaid_period character varying(40),
    allowed_change_plan_ids text,
    status public.offerstatus NOT NULL,
    description text,
    is_active boolean NOT NULL,
    default_ont_profile_id uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: collection_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collection_accounts (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    account_type public.collectionaccounttype NOT NULL,
    bank_name character varying(120),
    account_last4 character varying(4),
    currency character varying(3) NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: communication_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.communication_logs (
    id uuid NOT NULL,
    subscriber_id uuid,
    subscription_id uuid,
    channel public.communicationchannel NOT NULL,
    direction public.communicationdirection NOT NULL,
    recipient character varying(255),
    sender character varying(255),
    subject character varying(500),
    body text,
    status public.communicationstatus NOT NULL,
    sent_at timestamp with time zone,
    external_id character varying(200),
    splynx_message_id integer,
    metadata json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: connector_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.connector_configs (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    connector_type public.connectortype NOT NULL,
    base_url character varying(500),
    auth_type public.connectorauthtype NOT NULL,
    auth_config json,
    headers json,
    retry_policy json,
    timeout_sec integer,
    metadata json,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: contract_signatures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contract_signatures (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    service_order_id uuid,
    document_id uuid,
    signer_name character varying(200) NOT NULL,
    signer_email character varying(255) NOT NULL,
    signed_at timestamp with time zone NOT NULL,
    ip_address character varying(45) NOT NULL,
    user_agent character varying(500),
    agreement_text text NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: coverage_areas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.coverage_areas (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(80),
    zone_key character varying(80),
    buildout_status public.buildoutstatus NOT NULL,
    buildout_window character varying(120),
    serviceable boolean NOT NULL,
    priority integer NOT NULL,
    geometry_geojson json NOT NULL,
    geom public.geometry(Geometry,4326),
    min_latitude double precision,
    max_latitude double precision,
    min_longitude double precision,
    max_longitude double precision,
    constraints json,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: cpe_devices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cpe_devices (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    subscription_id uuid,
    service_address_id uuid,
    device_type public.devicetype NOT NULL,
    status public.cpe_devicestatus NOT NULL,
    serial_number character varying(120),
    model character varying(120),
    vendor character varying(120),
    mac_address character varying(64),
    installed_at timestamp with time zone,
    notes text,
    tr069_data_model character varying(40),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: credit_note_applications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_note_applications (
    id uuid NOT NULL,
    credit_note_id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    amount numeric(12,2) NOT NULL,
    memo text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: credit_note_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_note_lines (
    id uuid NOT NULL,
    credit_note_id uuid NOT NULL,
    description character varying(255) NOT NULL,
    quantity numeric(12,3) NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    amount numeric(12,2) NOT NULL,
    tax_rate_id uuid,
    tax_application public.taxapplication NOT NULL,
    metadata jsonb,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: credit_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_notes (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    invoice_id uuid,
    credit_number character varying(80),
    status public.creditnotestatus NOT NULL,
    currency character varying(3) NOT NULL,
    subtotal numeric(12,2) NOT NULL,
    tax_total numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    applied_total numeric(12,2) NOT NULL,
    memo text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: customer_notification_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_notification_events (
    id uuid NOT NULL,
    entity_type character varying(40) NOT NULL,
    entity_id uuid NOT NULL,
    channel character varying(40) NOT NULL,
    recipient character varying(255) NOT NULL,
    message text NOT NULL,
    status public.customernotificationstatus NOT NULL,
    sent_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: device_interfaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_interfaces (
    id uuid NOT NULL,
    device_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    description character varying(160),
    status public.interfacestatus NOT NULL,
    speed_mbps integer,
    mac_address character varying(64),
    snmp_index bigint,
    monitored boolean NOT NULL,
    last_in_octets double precision,
    last_out_octets double precision,
    last_counter_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: device_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_metrics (
    id uuid NOT NULL,
    device_id uuid NOT NULL,
    interface_id uuid,
    metric_type public.metrictype NOT NULL,
    value double precision NOT NULL,
    unit character varying(40),
    recorded_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: dns_threat_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dns_threat_events (
    id uuid NOT NULL,
    subscriber_id uuid,
    network_device_id uuid,
    pop_site_id uuid,
    queried_domain character varying(255) NOT NULL,
    query_type character varying(16),
    source_ip character varying(64),
    destination_ip character varying(64),
    threat_category character varying(80),
    threat_feed character varying(120),
    severity public.dnsthreatseverity NOT NULL,
    action public.dnsthreataction NOT NULL,
    confidence_score double precision,
    occurred_at timestamp with time zone NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: domain_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.domain_settings (
    id uuid NOT NULL,
    domain public.settingdomain NOT NULL,
    key character varying(120) NOT NULL,
    value_type public.settingvaluetype NOT NULL,
    value_text text,
    value_json json,
    is_secret boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_domain_settings_value_alignment CHECK ((((value_type = 'json'::public.settingvaluetype) AND (value_json IS NOT NULL) AND (value_text IS NULL)) OR ((value_type <> 'json'::public.settingvaluetype) AND (value_text IS NOT NULL))))
);


--
-- Name: dunning_action_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dunning_action_logs (
    id uuid NOT NULL,
    case_id uuid NOT NULL,
    invoice_id uuid,
    payment_id uuid,
    step_day integer,
    action public.dunningaction NOT NULL,
    outcome character varying(120),
    notes text,
    executed_at timestamp with time zone NOT NULL
);


--
-- Name: dunning_cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dunning_cases (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    policy_set_id uuid,
    status public.dunningcasestatus NOT NULL,
    current_step integer,
    started_at timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: enforcement_locks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.enforcement_locks (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    reason public.enforcementreason NOT NULL,
    source character varying(255) NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by character varying(255),
    notes text,
    CONSTRAINT ck_enforcement_locks_resolved_metadata CHECK (((is_active = true) OR ((resolved_at IS NOT NULL) AND (resolved_by IS NOT NULL))))
);


--
-- Name: eta_updates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.eta_updates (
    id uuid NOT NULL,
    service_order_id uuid NOT NULL,
    eta_at timestamp with time zone NOT NULL,
    note text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: event_store; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_store (
    id uuid NOT NULL,
    event_id uuid NOT NULL,
    event_type character varying(100) NOT NULL,
    payload jsonb NOT NULL,
    status public.eventstatus NOT NULL,
    retry_count integer NOT NULL,
    error text,
    processed_at timestamp with time zone,
    actor character varying(255),
    subscriber_id uuid,
    account_id uuid,
    subscription_id uuid,
    invoice_id uuid,
    service_order_id uuid,
    failed_handlers jsonb,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    is_active boolean NOT NULL
);


--
-- Name: external_references; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_references (
    id uuid NOT NULL,
    connector_config_id uuid,
    entity_type public.externalentitytype NOT NULL,
    entity_id uuid NOT NULL,
    external_id character varying(200) NOT NULL,
    external_url character varying(500),
    metadata json,
    last_synced_at timestamp with time zone,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fdh_cabinets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fdh_cabinets (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(80),
    region_id uuid,
    latitude double precision,
    longitude double precision,
    geom public.geometry(Point,4326),
    zone_id uuid,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fiber_access_points; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiber_access_points (
    id uuid NOT NULL,
    code character varying(60),
    name character varying(160) NOT NULL,
    access_point_type character varying(60),
    placement character varying(60),
    latitude double precision,
    longitude double precision,
    geom public.geometry(Point,4326),
    street character varying(200),
    city character varying(100),
    county character varying(100),
    state character varying(60),
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fiber_change_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiber_change_requests (
    id uuid NOT NULL,
    asset_type character varying(80) NOT NULL,
    asset_id uuid,
    operation public.fiberchangerequestoperation NOT NULL,
    payload json NOT NULL,
    status public.fiberchangerequeststatus NOT NULL,
    requested_by_person_id uuid,
    requested_by_vendor_id uuid,
    reviewed_by_person_id uuid,
    review_notes text,
    reviewed_at timestamp with time zone,
    applied_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fiber_segments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiber_segments (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    segment_type public.fibersegmenttype NOT NULL,
    cable_type public.fibercabletype,
    fiber_count integer,
    from_point_id uuid,
    to_point_id uuid,
    fiber_strand_id uuid,
    length_m double precision,
    route_geom public.geometry(LineString,4326),
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: COLUMN fiber_segments.fiber_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fiber_segments.fiber_count IS 'Number of fiber cores in the cable';


--
-- Name: fiber_splice_closures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiber_splice_closures (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    latitude double precision,
    longitude double precision,
    geom public.geometry(Point,4326),
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fiber_splice_trays; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiber_splice_trays (
    id uuid NOT NULL,
    closure_id uuid NOT NULL,
    tray_number integer NOT NULL,
    name character varying(160),
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fiber_splices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiber_splices (
    id uuid NOT NULL,
    closure_id uuid,
    from_strand_id uuid,
    to_strand_id uuid,
    tray_id uuid,
    "position" integer,
    splice_type character varying(80),
    loss_db double precision,
    notes text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: fiber_strands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiber_strands (
    id uuid NOT NULL,
    cable_name character varying(160) NOT NULL,
    strand_number integer NOT NULL,
    label character varying(160),
    status public.fiberstrandstatus NOT NULL,
    upstream_type public.fiberendpointtype,
    upstream_id uuid,
    downstream_type public.fiberendpointtype,
    downstream_id uuid,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fiber_termination_points; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiber_termination_points (
    id uuid NOT NULL,
    name character varying(160),
    endpoint_type public.odnendpointtype NOT NULL,
    ref_id uuid,
    latitude double precision,
    longitude double precision,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fup_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fup_policies (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    traffic_accounting_start time without time zone,
    traffic_accounting_end time without time zone,
    traffic_inverse_interval boolean NOT NULL,
    online_accounting_start time without time zone,
    online_accounting_end time without time zone,
    online_inverse_interval boolean NOT NULL,
    traffic_days_of_week integer[],
    online_days_of_week integer[],
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fup_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fup_rules (
    id uuid NOT NULL,
    policy_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    sort_order integer NOT NULL,
    consumption_period public.fupconsumptionperiod NOT NULL,
    direction public.fupdirection NOT NULL,
    threshold_amount double precision NOT NULL,
    threshold_unit public.fupdataunit NOT NULL,
    action public.fupaction NOT NULL,
    speed_reduction_percent double precision,
    enabled_by_rule_id uuid,
    cooldown_minutes integer NOT NULL,
    time_start time without time zone,
    time_end time without time zone,
    days_of_week integer[],
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: fup_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fup_states (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    offer_id uuid NOT NULL,
    active_rule_id uuid,
    action_status public.fupactionstatus NOT NULL,
    speed_reduction_percent double precision,
    original_profile_id uuid,
    throttle_profile_id uuid,
    cap_resets_at timestamp with time zone,
    last_evaluated_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: geo_areas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.geo_areas (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    area_type public.geoareatype NOT NULL,
    geometry_geojson json,
    geom public.geometry(Geometry,4326),
    min_latitude double precision,
    min_longitude double precision,
    max_latitude double precision,
    max_longitude double precision,
    metadata json,
    tags json,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: geo_layers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.geo_layers (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    layer_key character varying(80) NOT NULL,
    layer_type public.geolayertype NOT NULL,
    source_type public.geolayersource NOT NULL,
    style json,
    filters json,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: geo_locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.geo_locations (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    location_type public.geolocationtype NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    geom public.geometry(Point,4326),
    address_id uuid,
    pop_site_id uuid,
    metadata json,
    tags json,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: install_appointments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.install_appointments (
    id uuid NOT NULL,
    service_order_id uuid NOT NULL,
    scheduled_start timestamp with time zone NOT NULL,
    scheduled_end timestamp with time zone NOT NULL,
    technician character varying(120),
    status public.appointmentstatus NOT NULL,
    notes text,
    is_self_install boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: integration_connectors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_connectors (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    version character varying(32) NOT NULL,
    connector_type public.integrationconnectortype NOT NULL,
    status public.integrationconnectorstatus NOT NULL,
    configuration json,
    last_sync_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: integration_hook_executions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_hook_executions (
    id uuid NOT NULL,
    hook_id uuid NOT NULL,
    event_type character varying(120) NOT NULL,
    status public.integrationhookexecutionstatus NOT NULL,
    latency_ms integer,
    response_status integer,
    payload json,
    response_body text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: integration_hooks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_hooks (
    id uuid NOT NULL,
    title character varying(180) NOT NULL,
    hook_type public.integrationhooktype NOT NULL,
    command text,
    url character varying(600),
    http_method character varying(10) NOT NULL,
    auth_type public.integrationhookauthtype NOT NULL,
    auth_config json,
    retry_max integer NOT NULL,
    retry_backoff_ms integer NOT NULL,
    event_filters json,
    is_enabled boolean NOT NULL,
    notes text,
    last_triggered_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: integration_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_jobs (
    id uuid NOT NULL,
    target_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    job_type public.integrationjobtype NOT NULL,
    schedule_type public.integrationscheduletype NOT NULL,
    interval_minutes integer,
    interval_seconds integer,
    is_active boolean NOT NULL,
    last_run_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: integration_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_runs (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    status public.integrationrunstatus NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    error text,
    metrics json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: integration_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_targets (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    target_type public.integrationtargettype NOT NULL,
    connector_config_id uuid,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: invoice_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoice_lines (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    subscription_id uuid,
    description character varying(255) NOT NULL,
    quantity numeric(12,3) NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    amount numeric(12,2) NOT NULL,
    tax_rate_id uuid,
    tax_application public.taxapplication NOT NULL,
    metadata jsonb,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: invoice_pdf_exports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoice_pdf_exports (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    status public.invoicepdfexportstatus NOT NULL,
    requested_by_id uuid,
    celery_task_id character varying(120),
    file_path character varying(500),
    file_size_bytes integer,
    error text,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoices (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    invoice_number character varying(80),
    status public.invoicestatus NOT NULL,
    currency character varying(3) NOT NULL,
    subtotal numeric(12,2) NOT NULL,
    tax_total numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    balance_due numeric(12,2) NOT NULL,
    billing_period_start timestamp with time zone,
    billing_period_end timestamp with time zone,
    issued_at timestamp with time zone,
    due_at timestamp with time zone,
    paid_at timestamp with time zone,
    memo text,
    is_proforma boolean NOT NULL,
    is_sent boolean,
    added_by_id uuid,
    splynx_invoice_id integer,
    metadata jsonb,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ip_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ip_assignments (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    subscription_id uuid,
    subscription_add_on_id uuid,
    service_address_id uuid,
    ip_version public.ipversion NOT NULL,
    ipv4_address_id uuid,
    ipv6_address_id uuid,
    prefix_length integer,
    gateway character varying(64),
    dns_primary character varying(64),
    dns_secondary character varying(64),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_ip_assignments_version_address CHECK ((((ip_version = 'ipv4'::public.ipversion) AND (ipv4_address_id IS NOT NULL) AND (ipv6_address_id IS NULL)) OR ((ip_version = 'ipv6'::public.ipversion) AND (ipv6_address_id IS NOT NULL) AND (ipv4_address_id IS NULL))))
);


--
-- Name: ip_blocks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ip_blocks (
    id uuid NOT NULL,
    pool_id uuid NOT NULL,
    cidr character varying(64) NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ip_pools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ip_pools (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    ip_version public.ipversion NOT NULL,
    cidr character varying(64) NOT NULL,
    gateway character varying(64),
    dns_primary character varying(64),
    dns_secondary character varying(64),
    is_active boolean NOT NULL,
    olt_device_id uuid,
    nas_device_id uuid,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ipv4_addresses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ipv4_addresses (
    id uuid NOT NULL,
    address character varying(15) NOT NULL,
    pool_id uuid,
    is_reserved boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ipv6_addresses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ipv6_addresses (
    id uuid NOT NULL,
    address character varying(64) NOT NULL,
    pool_id uuid,
    is_reserved boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: jump_hosts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jump_hosts (
    id uuid NOT NULL,
    name character varying(255) NOT NULL,
    hostname character varying(255) NOT NULL,
    port integer NOT NULL,
    username character varying(255) NOT NULL,
    ssh_key text,
    ssh_password character varying(512),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: kpi_aggregates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kpi_aggregates (
    id uuid NOT NULL,
    key character varying(120) NOT NULL,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    value numeric(14,4) NOT NULL,
    metadata json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: kpi_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kpi_configs (
    id uuid NOT NULL,
    key character varying(120) NOT NULL,
    name character varying(160) NOT NULL,
    description text,
    parameters json,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ledger_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ledger_entries (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    invoice_id uuid,
    payment_id uuid,
    entry_type public.ledgerentrytype NOT NULL,
    source public.ledgersource NOT NULL,
    category public.ledgercategory,
    amount numeric(12,2) NOT NULL,
    currency character varying(3) NOT NULL,
    memo text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: legal_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.legal_documents (
    id uuid NOT NULL,
    document_type public.legaldocumenttype NOT NULL,
    title character varying(200) NOT NULL,
    slug character varying(100) NOT NULL,
    version character varying(20) NOT NULL,
    summary text,
    content text,
    file_path character varying(500),
    file_name character varying(255),
    file_size integer,
    mime_type character varying(100),
    is_current boolean NOT NULL,
    is_published boolean NOT NULL,
    published_at timestamp with time zone,
    effective_date timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: mfa_methods; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mfa_methods (
    id uuid NOT NULL,
    subscriber_id uuid,
    system_user_id uuid,
    method_type public.mfamethodtype NOT NULL,
    label character varying(120),
    secret character varying(255),
    phone character varying(40),
    email character varying(255),
    is_primary boolean NOT NULL,
    enabled boolean NOT NULL,
    is_active boolean NOT NULL,
    verified_at timestamp with time zone,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_mfa_methods_exactly_one_principal CHECK (((subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)))
);


--
-- Name: mrr_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mrr_snapshots (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    snapshot_date date NOT NULL,
    mrr_amount numeric(12,2) NOT NULL,
    currency character varying(3) NOT NULL,
    active_subscriptions integer NOT NULL,
    splynx_customer_id integer,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: nas_config_backups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nas_config_backups (
    id uuid NOT NULL,
    nas_device_id uuid NOT NULL,
    config_content text NOT NULL,
    config_hash character varying(64),
    config_format character varying(40),
    config_size_bytes integer,
    backup_method public.configbackupmethod,
    is_scheduled boolean NOT NULL,
    is_manual boolean NOT NULL,
    has_changes boolean NOT NULL,
    changes_summary text,
    is_current boolean NOT NULL,
    keep_forever boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(120)
);


--
-- Name: nas_connection_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nas_connection_rules (
    id uuid NOT NULL,
    nas_device_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    connection_type public.connectiontype,
    ip_assignment_mode character varying(40),
    rate_limit_profile character varying(120),
    match_expression character varying(255),
    priority integer NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: nas_devices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nas_devices (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(60),
    vendor public.nasvendor NOT NULL,
    model character varying(120),
    serial_number character varying(120),
    firmware_version character varying(80),
    description text,
    pop_site_id uuid,
    rack_position character varying(40),
    ip_address character varying(64),
    management_ip character varying(64),
    management_port integer,
    nas_ip character varying(64),
    shared_secret character varying(255),
    coa_port integer,
    ssh_username character varying(120),
    ssh_password character varying(255),
    ssh_key text,
    ssh_verify_host_key boolean NOT NULL,
    api_username character varying(120),
    api_password character varying(255),
    api_token text,
    api_url character varying(500),
    api_verify_tls boolean NOT NULL,
    snmp_community character varying(120),
    snmp_version character varying(10),
    snmp_port integer,
    supported_connection_types jsonb,
    default_connection_type public.connectiontype,
    backup_enabled boolean NOT NULL,
    backup_method public.configbackupmethod,
    backup_schedule character varying(60),
    last_backup_at timestamp with time zone,
    status public.nasdevicestatus NOT NULL,
    is_active boolean NOT NULL,
    last_seen_at timestamp with time zone,
    max_concurrent_subscribers integer,
    current_subscriber_count integer NOT NULL,
    health_status public.healthstatus NOT NULL,
    last_health_check_at timestamp with time zone,
    notes text,
    tags jsonb,
    network_device_id uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: network_device_bandwidth_graph_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_device_bandwidth_graph_sources (
    id uuid NOT NULL,
    graph_id uuid NOT NULL,
    source_device_id uuid NOT NULL,
    snmp_oid_id uuid NOT NULL,
    factor double precision NOT NULL,
    color_hex character varying(7) NOT NULL,
    draw_type character varying(16) NOT NULL,
    stack_enabled boolean NOT NULL,
    value_unit character varying(12) NOT NULL,
    sort_order integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: network_device_bandwidth_graphs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_device_bandwidth_graphs (
    id uuid NOT NULL,
    device_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    vertical_axis_title character varying(80) NOT NULL,
    height_px integer NOT NULL,
    is_public boolean NOT NULL,
    public_token character varying(64),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: network_device_snmp_oids; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_device_snmp_oids (
    id uuid NOT NULL,
    device_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    oid character varying(160) NOT NULL,
    check_interval_seconds integer NOT NULL,
    rrd_data_source_type character varying(16) NOT NULL,
    is_enabled boolean NOT NULL,
    last_poll_status character varying(16),
    last_polled_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: network_devices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_devices (
    id uuid NOT NULL,
    pop_site_id uuid,
    parent_device_id uuid,
    name character varying(160) NOT NULL,
    hostname character varying(160),
    mgmt_ip character varying(64),
    vendor character varying(120),
    model character varying(120),
    serial_number character varying(120),
    device_type public.devicetype,
    role public.devicerole NOT NULL,
    status public.monitoring_devicestatus NOT NULL,
    ping_enabled boolean NOT NULL,
    snmp_enabled boolean NOT NULL,
    send_notifications boolean NOT NULL,
    notification_delay_minutes integer NOT NULL,
    snmp_port integer,
    snmp_version character varying(10),
    snmp_community character varying(255),
    snmp_rw_community character varying(255),
    snmp_username character varying(120),
    snmp_auth_protocol character varying(16),
    snmp_auth_secret character varying(255),
    snmp_priv_protocol character varying(16),
    snmp_priv_secret character varying(255),
    last_ping_at timestamp with time zone,
    last_ping_ok boolean,
    ping_down_since timestamp with time zone,
    last_snmp_at timestamp with time zone,
    last_snmp_ok boolean,
    snmp_down_since timestamp with time zone,
    notes text,
    splynx_monitoring_id integer,
    is_active boolean NOT NULL,
    max_concurrent_subscribers integer,
    current_subscriber_count integer NOT NULL,
    health_status public.healthstatus NOT NULL,
    last_health_check_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: network_operations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_operations (
    id uuid NOT NULL,
    operation_type public.networkoperationtype NOT NULL,
    target_type public.networkoperationtargettype NOT NULL,
    target_id uuid NOT NULL,
    parent_id uuid,
    status public.networkoperationstatus NOT NULL,
    correlation_key character varying(255),
    waiting_reason text,
    input_payload json,
    output_payload json,
    error text,
    retry_count integer NOT NULL,
    max_retries integer NOT NULL,
    initiated_by character varying(120),
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: network_topology_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_topology_links (
    id uuid NOT NULL,
    source_device_id uuid NOT NULL,
    source_interface_id uuid,
    target_device_id uuid NOT NULL,
    target_interface_id uuid,
    link_role public.topologylinkrole NOT NULL,
    medium public.topologylinkmedium NOT NULL,
    capacity_bps bigint,
    bundle_key character varying(80),
    topology_group character varying(80),
    admin_status public.topologylinkadminstatus NOT NULL,
    is_active boolean NOT NULL,
    discovered_at timestamp with time zone,
    confirmed_by character varying(120),
    notes text,
    metadata json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: network_zones; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_zones (
    id uuid NOT NULL,
    name character varying(100) NOT NULL,
    description text,
    parent_id uuid,
    latitude double precision,
    longitude double precision,
    geom public.geometry(Point,4326),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: notification_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_deliveries (
    id uuid NOT NULL,
    notification_id uuid NOT NULL,
    provider character varying(120),
    provider_message_id character varying(200),
    status public.deliverystatus NOT NULL,
    response_code character varying(60),
    response_body text,
    occurred_at timestamp with time zone NOT NULL,
    is_active boolean NOT NULL
);


--
-- Name: notification_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_templates (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    code character varying(120) NOT NULL,
    channel public.notificationchannel NOT NULL,
    subject character varying(200),
    body text NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    id uuid NOT NULL,
    template_id uuid,
    connector_config_id uuid,
    channel public.notificationchannel NOT NULL,
    recipient character varying(255) NOT NULL,
    subject character varying(200),
    body text,
    status public.notificationstatus NOT NULL,
    send_at timestamp with time zone,
    sent_at timestamp with time zone,
    last_error text,
    retry_count integer NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: oauth_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_tokens (
    id uuid NOT NULL,
    connector_config_id uuid NOT NULL,
    provider character varying(64) NOT NULL,
    account_type character varying(64) NOT NULL,
    external_account_id character varying(120) NOT NULL,
    external_account_name character varying(255),
    access_token text,
    refresh_token text,
    token_type character varying(64),
    token_expires_at timestamp with time zone,
    scopes json,
    last_refreshed_at timestamp with time zone,
    refresh_error text,
    is_active boolean NOT NULL,
    metadata json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: offer_add_ons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_add_ons (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    add_on_id uuid NOT NULL,
    is_required boolean NOT NULL,
    min_quantity integer,
    max_quantity integer
);


--
-- Name: offer_billing_mode_availability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_billing_mode_availability (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    billing_mode public.billingmode NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: offer_category_availability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_category_availability (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    subscriber_category public.subscribercategory NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: offer_location_availability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_location_availability (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    pop_site_id uuid NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: offer_prices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_prices (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    price_type public.pricetype NOT NULL,
    amount numeric(10,2) NOT NULL,
    currency character varying(3) NOT NULL,
    billing_cycle public.billingcycle,
    unit public.priceunit,
    description character varying(200),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: offer_radius_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_radius_profiles (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    profile_id uuid NOT NULL
);


--
-- Name: offer_reseller_availability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_reseller_availability (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    reseller_id uuid NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: offer_version_prices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_version_prices (
    id uuid NOT NULL,
    offer_version_id uuid NOT NULL,
    price_type public.pricetype NOT NULL,
    amount numeric(10,2) NOT NULL,
    currency character varying(3) NOT NULL,
    billing_cycle public.billingcycle,
    unit public.priceunit,
    description character varying(200),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: offer_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_versions (
    id uuid NOT NULL,
    offer_id uuid NOT NULL,
    version_number integer NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(60),
    service_type public.servicetype NOT NULL,
    access_type public.accesstype NOT NULL,
    price_basis public.pricebasis NOT NULL,
    billing_cycle public.billingcycle NOT NULL,
    contract_term public.contractterm NOT NULL,
    region_zone_id uuid,
    usage_allowance_id uuid,
    sla_profile_id uuid,
    policy_set_id uuid,
    status public.offerstatus NOT NULL,
    description text,
    effective_start timestamp with time zone,
    effective_end timestamp with time zone,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: olt_autofind_candidates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_autofind_candidates (
    id uuid NOT NULL,
    olt_id uuid NOT NULL,
    ont_unit_id uuid,
    fsp character varying(32) NOT NULL,
    serial_number character varying(120) NOT NULL,
    serial_hex character varying(32),
    vendor_id character varying(32),
    model character varying(120),
    software_version character varying(160),
    mac character varying(32),
    equipment_sn character varying(120),
    autofind_time character varying(120),
    is_active boolean NOT NULL,
    resolution_reason character varying(64),
    notes text,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: olt_card_ports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_card_ports (
    id uuid NOT NULL,
    card_id uuid NOT NULL,
    port_number integer NOT NULL,
    name character varying(120),
    port_type public.oltporttype NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: olt_cards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_cards (
    id uuid NOT NULL,
    shelf_id uuid NOT NULL,
    slot_number integer NOT NULL,
    card_type character varying(120),
    model character varying(120),
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: olt_config_backups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_config_backups (
    id uuid NOT NULL,
    olt_device_id uuid NOT NULL,
    backup_type public.oltconfigbackuptype NOT NULL,
    file_path character varying(512) NOT NULL,
    file_size_bytes integer,
    file_hash character varying(64),
    notes text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: olt_devices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_devices (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    hostname character varying(160),
    mgmt_ip character varying(64),
    vendor character varying(120),
    model character varying(120),
    serial_number character varying(120),
    firmware_version character varying(120),
    software_version character varying(120),
    ssh_username character varying(120),
    ssh_password character varying(255),
    ssh_port integer,
    snmp_enabled boolean NOT NULL,
    snmp_port integer,
    snmp_version character varying(10),
    snmp_ro_community character varying(255),
    snmp_rw_community character varying(255),
    netconf_enabled boolean NOT NULL,
    netconf_port integer,
    tr069_acs_server_id uuid,
    tr069_profiles_snapshot json,
    tr069_profiles_snapshot_at timestamp with time zone,
    supported_pon_types character varying(120),
    notes text,
    status public.devicestatus NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: olt_firmware_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_firmware_images (
    id uuid NOT NULL,
    vendor character varying(120) NOT NULL,
    model character varying(120),
    version character varying(120) NOT NULL,
    file_url character varying(500) NOT NULL,
    filename character varying(255),
    checksum character varying(128),
    file_size_bytes integer,
    release_notes text,
    upgrade_method character varying(60),
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: olt_power_units; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_power_units (
    id uuid NOT NULL,
    olt_id uuid NOT NULL,
    slot character varying(40) NOT NULL,
    status public.hardwareunitstatus,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: olt_sfp_modules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_sfp_modules (
    id uuid NOT NULL,
    olt_card_port_id uuid NOT NULL,
    vendor character varying(120),
    model character varying(120),
    serial_number character varying(120),
    wavelength_nm integer,
    rx_power_dbm double precision,
    tx_power_dbm double precision,
    installed_at timestamp with time zone,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: olt_shelves; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.olt_shelves (
    id uuid NOT NULL,
    olt_id uuid NOT NULL,
    shelf_number integer NOT NULL,
    label character varying(120),
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: on_call_rotation_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.on_call_rotation_members (
    id uuid NOT NULL,
    rotation_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    contact character varying(255) NOT NULL,
    priority integer NOT NULL,
    last_used_at timestamp with time zone,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: on_call_rotations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.on_call_rotations (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    timezone character varying(60) NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ont_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ont_assignments (
    id uuid NOT NULL,
    ont_unit_id uuid NOT NULL,
    pon_port_id uuid,
    subscriber_id uuid,
    subscription_id uuid,
    service_address_id uuid,
    assigned_at timestamp with time zone,
    active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ont_firmware_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ont_firmware_images (
    id uuid NOT NULL,
    vendor character varying(120) NOT NULL,
    model character varying(120),
    version character varying(120) NOT NULL,
    file_url character varying(500) NOT NULL,
    filename character varying(255),
    checksum character varying(128),
    file_size_bytes integer,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ont_profile_wan_services; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ont_profile_wan_services (
    id uuid NOT NULL,
    profile_id uuid NOT NULL,
    service_type public.wanservicetype NOT NULL,
    name character varying(120),
    priority integer NOT NULL,
    vlan_mode public.vlanmode NOT NULL,
    s_vlan integer,
    c_vlan integer,
    cos_priority integer,
    mtu integer NOT NULL,
    connection_type public.wanconnectiontype NOT NULL,
    nat_enabled boolean NOT NULL,
    ip_mode public.ipprotocol,
    pppoe_username_template character varying(200),
    pppoe_password_mode public.pppoepasswordmode,
    pppoe_static_password character varying(500),
    static_ip_source character varying(200),
    bind_lan_ports json,
    bind_ssid_index integer,
    gem_port_id integer,
    t_cont_profile character varying(120),
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ont_provisioning_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ont_provisioning_profiles (
    id uuid NOT NULL,
    owner_subscriber_id uuid,
    name character varying(120) NOT NULL,
    profile_type public.ontprofiletype NOT NULL,
    description text,
    config_method public.configmethod,
    onu_mode public.onumode,
    ip_protocol public.ipprotocol,
    download_speed_profile_id uuid,
    upload_speed_profile_id uuid,
    mgmt_ip_mode public.mgmtipmode,
    mgmt_vlan_tag integer,
    mgmt_remote_access boolean NOT NULL,
    wifi_enabled boolean NOT NULL,
    wifi_ssid_template character varying(120),
    wifi_security_mode character varying(40),
    wifi_channel character varying(10),
    wifi_band character varying(20),
    internet_config_ip_index integer,
    wan_config_profile_id integer,
    pppoe_omci_vlan integer,
    cr_username character varying(120),
    cr_password character varying(120),
    voip_enabled boolean NOT NULL,
    is_default boolean NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ont_units; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ont_units (
    id uuid NOT NULL,
    serial_number character varying(120) NOT NULL,
    model character varying(120),
    vendor character varying(120),
    firmware_version character varying(120),
    notes text,
    is_active boolean NOT NULL,
    onu_rx_signal_dbm double precision,
    olt_rx_signal_dbm double precision,
    distance_meters integer,
    onu_tx_signal_dbm double precision,
    ont_temperature_c double precision,
    ont_voltage_v double precision,
    ont_bias_current_ma double precision,
    signal_updated_at timestamp with time zone,
    online_status public.onuonlinestatus DEFAULT 'unknown'::public.onuonlinestatus NOT NULL,
    last_seen_at timestamp with time zone,
    offline_reason public.onuofflinereason,
    zone_id uuid,
    onu_type_id uuid,
    olt_device_id uuid,
    pon_type public.pontype,
    gpon_channel public.gponchannel,
    board character varying(60),
    port character varying(60),
    onu_mode public.onumode,
    user_vlan_id uuid,
    splitter_id uuid,
    splitter_port_id uuid,
    download_speed_profile_id uuid,
    upload_speed_profile_id uuid,
    name character varying(200),
    address_or_comment text,
    external_id character varying(120),
    use_gps boolean NOT NULL,
    gps_latitude double precision,
    gps_longitude double precision,
    wan_vlan_id uuid,
    wan_mode public.wanmode,
    config_method public.configmethod,
    ip_protocol public.ipprotocol,
    pppoe_username character varying(120),
    pppoe_password character varying(120),
    mac_address character varying(64),
    observed_wan_ip character varying(64),
    observed_pppoe_status character varying(60),
    observed_lan_mode character varying(60),
    observed_wifi_clients integer,
    observed_lan_hosts integer,
    observed_runtime_updated_at timestamp with time zone,
    olt_observed_snapshot json,
    olt_observed_snapshot_at timestamp with time zone,
    wan_remote_access boolean NOT NULL,
    tr069_acs_server_id uuid,
    mgmt_ip_mode public.mgmtipmode,
    mgmt_vlan_id uuid,
    mgmt_ip_address character varying(64),
    mgmt_remote_access boolean NOT NULL,
    voip_enabled boolean NOT NULL,
    provisioning_profile_id uuid,
    provisioning_status public.ontprovisioningstatus,
    last_provisioned_at timestamp with time zone,
    tr069_data_model character varying(40),
    last_sync_source character varying(40),
    last_sync_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: onu_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.onu_types (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    pon_type public.pontype NOT NULL,
    gpon_channel public.gponchannel NOT NULL,
    ethernet_ports integer NOT NULL,
    wifi_ports integer NOT NULL,
    voip_ports integer NOT NULL,
    catv_ports integer NOT NULL,
    allow_custom_profiles boolean NOT NULL,
    capability public.onucapability NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: payment_allocations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_allocations (
    id uuid NOT NULL,
    payment_id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    amount numeric(12,2) NOT NULL,
    memo text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: payment_arrangement_installments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_arrangement_installments (
    id uuid NOT NULL,
    arrangement_id uuid NOT NULL,
    installment_number integer NOT NULL,
    amount numeric(12,2) NOT NULL,
    due_date date NOT NULL,
    paid_at timestamp with time zone,
    payment_id uuid,
    status public.installmentstatus NOT NULL,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: payment_arrangements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_arrangements (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    invoice_id uuid,
    total_amount numeric(12,2) NOT NULL,
    installment_amount numeric(12,2) NOT NULL,
    frequency public.paymentfrequency NOT NULL,
    installments_total integer NOT NULL,
    installments_paid integer NOT NULL,
    start_date date NOT NULL,
    end_date date,
    next_due_date date,
    status public.arrangementstatus NOT NULL,
    requested_by_subscriber_id uuid,
    approved_by_subscriber_id uuid,
    approved_at timestamp with time zone,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: payment_channel_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_channel_accounts (
    id uuid NOT NULL,
    channel_id uuid NOT NULL,
    collection_account_id uuid NOT NULL,
    currency character varying(3),
    priority integer NOT NULL,
    is_default boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: payment_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_channels (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    channel_type public.paymentchanneltype NOT NULL,
    provider_id uuid,
    default_collection_account_id uuid,
    fee_rules json,
    is_active boolean NOT NULL,
    is_default boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: payment_methods; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_methods (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    payment_channel_id uuid,
    method_type public.paymentmethodtype NOT NULL,
    label character varying(120),
    token character varying(255),
    last4 character varying(4),
    brand character varying(40),
    expires_month integer,
    expires_year integer,
    is_default boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: payment_provider_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_provider_events (
    id uuid NOT NULL,
    provider_id uuid NOT NULL,
    payment_id uuid,
    invoice_id uuid,
    event_type character varying(120) NOT NULL,
    external_id character varying(160),
    idempotency_key character varying(160),
    status public.paymentprovidereventstatus NOT NULL,
    payload json,
    error text,
    received_at timestamp with time zone NOT NULL,
    processed_at timestamp with time zone
);


--
-- Name: payment_providers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_providers (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    provider_type public.paymentprovidertype NOT NULL,
    connector_config_id uuid,
    webhook_secret_ref character varying(255),
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payments (
    id uuid NOT NULL,
    account_id uuid NOT NULL,
    payment_method_id uuid,
    payment_channel_id uuid,
    collection_account_id uuid,
    provider_id uuid,
    amount numeric(12,2) NOT NULL,
    currency character varying(3) NOT NULL,
    status public.paymentstatus NOT NULL,
    paid_at timestamp with time zone,
    external_id character varying(120),
    memo text,
    receipt_number character varying(120),
    splynx_payment_id integer,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.permissions (
    id uuid NOT NULL,
    key character varying(120) NOT NULL,
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: policy_dunning_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_dunning_steps (
    id uuid NOT NULL,
    policy_set_id uuid NOT NULL,
    day_offset integer NOT NULL,
    action public.dunningaction NOT NULL,
    note character varying(200)
);


--
-- Name: policy_sets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_sets (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    proration_policy public.prorationpolicy NOT NULL,
    downgrade_policy public.prorationpolicy NOT NULL,
    trial_days integer,
    trial_card_required boolean NOT NULL,
    grace_days integer,
    suspension_action public.suspensionaction NOT NULL,
    refund_policy public.refundpolicy NOT NULL,
    refund_window_days integer,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: pon_port_splitter_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pon_port_splitter_links (
    id uuid NOT NULL,
    pon_port_id uuid NOT NULL,
    splitter_port_id uuid NOT NULL,
    active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: pon_ports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pon_ports (
    id uuid NOT NULL,
    olt_id uuid NOT NULL,
    olt_card_port_id uuid,
    name character varying(120) NOT NULL,
    port_number integer,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: pop_site_contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pop_site_contacts (
    id uuid NOT NULL,
    pop_site_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    role character varying(120),
    phone character varying(40),
    email character varying(255),
    notes text,
    is_primary boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: pop_sites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pop_sites (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(60),
    address_line1 character varying(120),
    address_line2 character varying(120),
    city character varying(80),
    region character varying(80),
    postal_code character varying(20),
    country_code character varying(2),
    latitude double precision,
    longitude double precision,
    geom public.geometry(Point,4326),
    zone_id uuid,
    owner_subscriber_id uuid,
    reseller_id uuid,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: port_vlans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.port_vlans (
    id uuid NOT NULL,
    port_id uuid NOT NULL,
    vlan_id uuid NOT NULL,
    is_tagged boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: portal_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_messages (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    message_type public.portalmessagetype NOT NULL,
    subject character varying(255) NOT NULL,
    body text NOT NULL,
    status public.portalmessagestatus NOT NULL,
    is_pinned boolean NOT NULL,
    read_at timestamp with time zone,
    expires_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: portal_onboarding_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_onboarding_states (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    steps_completed integer NOT NULL,
    is_complete boolean NOT NULL,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: ports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ports (
    id uuid NOT NULL,
    device_id uuid NOT NULL,
    port_number integer,
    name character varying(80) NOT NULL,
    port_type public.porttype NOT NULL,
    status public.portstatus NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: provisioning_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provisioning_logs (
    id uuid NOT NULL,
    nas_device_id uuid,
    subscriber_id uuid,
    subscription_id uuid,
    template_id uuid,
    action public.provisioningaction NOT NULL,
    command_sent text,
    response_received text,
    status public.provisioninglogstatus NOT NULL,
    error_message text,
    execution_time_ms integer,
    triggered_by character varying(120),
    request_data jsonb,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: provisioning_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provisioning_runs (
    id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    service_order_id uuid,
    subscription_id uuid,
    status public.provisioningrunstatus NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    input_payload json,
    output_payload json,
    error_message text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: provisioning_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provisioning_steps (
    id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    step_type public.provisioningsteptype NOT NULL,
    order_index integer NOT NULL,
    config json,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: provisioning_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provisioning_tasks (
    id uuid NOT NULL,
    service_order_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    status public.provisioning_taskstatus NOT NULL,
    assigned_to character varying(120),
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: provisioning_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provisioning_templates (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(80),
    vendor public.nasvendor NOT NULL,
    connection_type public.connectiontype NOT NULL,
    action public.provisioningaction NOT NULL,
    template_content text NOT NULL,
    description text,
    placeholders jsonb,
    execution_method public.executionmethod,
    expected_output text,
    timeout_seconds integer,
    is_active boolean NOT NULL,
    is_default boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: provisioning_workflows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provisioning_workflows (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    vendor public.provisioningvendor NOT NULL,
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: queue_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.queue_mappings (
    id uuid NOT NULL,
    nas_device_id uuid NOT NULL,
    queue_name character varying(255) NOT NULL,
    subscription_id uuid NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: quota_buckets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quota_buckets (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    included_gb numeric(10,2),
    used_gb numeric(10,2) NOT NULL,
    rollover_gb numeric(10,2) NOT NULL,
    overage_gb numeric(10,2) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: radius_accounting_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_accounting_sessions (
    id uuid NOT NULL,
    subscription_id uuid,
    access_credential_id uuid,
    radius_client_id uuid,
    nas_device_id uuid,
    session_id character varying(120) NOT NULL,
    status_type public.accountingstatus NOT NULL,
    session_start timestamp with time zone,
    session_end timestamp with time zone,
    input_octets bigint,
    output_octets bigint,
    terminate_cause character varying(120),
    splynx_session_id integer,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: radius_active_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_active_sessions (
    id uuid NOT NULL,
    subscriber_id uuid,
    subscription_id uuid,
    access_credential_id uuid,
    nas_device_id uuid,
    username character varying(120) NOT NULL,
    acct_session_id character varying(120) NOT NULL,
    nas_ip_address character varying(64),
    framed_ip_address character varying(64),
    framed_ipv6_prefix character varying(128),
    calling_station_id character varying(64),
    nas_port_id character varying(120),
    session_start timestamp with time zone NOT NULL,
    session_time integer NOT NULL,
    bytes_in bigint NOT NULL,
    bytes_out bigint NOT NULL,
    packets_in bigint NOT NULL,
    packets_out bigint NOT NULL,
    last_update timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: radius_attributes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_attributes (
    id uuid NOT NULL,
    profile_id uuid NOT NULL,
    attribute character varying(120) NOT NULL,
    operator character varying(10),
    value character varying(255) NOT NULL
);


--
-- Name: radius_auth_errors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_auth_errors (
    id uuid NOT NULL,
    subscriber_id uuid,
    subscription_id uuid,
    nas_device_id uuid,
    username character varying(120) NOT NULL,
    nas_ip_address character varying(64),
    calling_station_id character varying(64),
    error_type public.radiusautherrortype NOT NULL,
    reply_message character varying(255),
    detail text,
    occurred_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: radius_clients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_clients (
    id uuid NOT NULL,
    server_id uuid NOT NULL,
    nas_device_id uuid,
    client_ip character varying(64) NOT NULL,
    shared_secret_hash character varying(255) NOT NULL,
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: radius_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_profiles (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(80),
    vendor public.nasvendor NOT NULL,
    connection_type public.connectiontype,
    description text,
    download_speed integer,
    upload_speed integer,
    burst_download integer,
    burst_upload integer,
    burst_threshold integer,
    burst_time integer,
    vlan_id integer,
    inner_vlan_id integer,
    ip_pool_name character varying(120),
    ipv6_pool_name character varying(120),
    session_timeout integer,
    idle_timeout integer,
    simultaneous_use integer,
    mikrotik_rate_limit character varying(255),
    mikrotik_address_list character varying(120),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: radius_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_servers (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    host character varying(255) NOT NULL,
    auth_port integer NOT NULL,
    acct_port integer NOT NULL,
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: radius_sync_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_sync_jobs (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    server_id uuid NOT NULL,
    connector_config_id uuid,
    sync_users boolean NOT NULL,
    sync_nas_clients boolean NOT NULL,
    is_active boolean NOT NULL,
    last_run_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: radius_sync_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_sync_runs (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    status public.radiussyncstatus NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    users_created integer NOT NULL,
    users_updated integer NOT NULL,
    clients_created integer NOT NULL,
    clients_updated integer NOT NULL,
    details json
);


--
-- Name: radius_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.radius_users (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    subscription_id uuid,
    access_credential_id uuid NOT NULL,
    username character varying(120) NOT NULL,
    secret_hash character varying(255),
    radius_profile_id uuid,
    is_active boolean NOT NULL,
    last_sync_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: region_zones; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.region_zones (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    code character varying(40),
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: reseller_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reseller_users (
    id uuid NOT NULL,
    person_id uuid,
    reseller_id uuid,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: resellers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resellers (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(60),
    contact_email character varying(255),
    contact_phone character varying(40),
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: role_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role_permissions (
    id uuid NOT NULL,
    role_id uuid NOT NULL,
    permission_id uuid NOT NULL
);


--
-- Name: roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.roles (
    id uuid NOT NULL,
    name character varying(80) NOT NULL,
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: router_config_push_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.router_config_push_results (
    id uuid NOT NULL,
    push_id uuid NOT NULL,
    router_id uuid NOT NULL,
    status public.routerpushresultstatus NOT NULL,
    response_data json,
    error_message text,
    pre_snapshot_id uuid,
    post_snapshot_id uuid,
    duration_ms integer,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: router_config_pushes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.router_config_pushes (
    id uuid NOT NULL,
    template_id uuid,
    commands json NOT NULL,
    variable_values json,
    initiated_by uuid NOT NULL,
    status public.routerconfigpushstatus NOT NULL,
    created_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: router_config_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.router_config_snapshots (
    id uuid NOT NULL,
    router_id uuid NOT NULL,
    config_export text NOT NULL,
    config_hash character varying(64) NOT NULL,
    source public.routersnapshotsource NOT NULL,
    captured_by uuid,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: router_config_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.router_config_templates (
    id uuid NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    template_body text NOT NULL,
    category public.routertemplatecategory NOT NULL,
    variables json NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: router_interfaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.router_interfaces (
    id uuid NOT NULL,
    router_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    type character varying(50) NOT NULL,
    mac_address character varying(17),
    is_running boolean NOT NULL,
    is_disabled boolean NOT NULL,
    rx_byte bigint NOT NULL,
    tx_byte bigint NOT NULL,
    rx_packet bigint NOT NULL,
    tx_packet bigint NOT NULL,
    last_link_up_time character varying(100),
    speed character varying(50),
    comment character varying(255),
    synced_at timestamp with time zone NOT NULL
);


--
-- Name: routers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.routers (
    id uuid NOT NULL,
    name character varying(255) NOT NULL,
    hostname character varying(255) NOT NULL,
    management_ip character varying(255) NOT NULL,
    rest_api_port integer NOT NULL,
    rest_api_username character varying(255) NOT NULL,
    rest_api_password character varying(512) NOT NULL,
    use_ssl boolean NOT NULL,
    verify_tls boolean NOT NULL,
    routeros_version character varying(50),
    board_name character varying(100),
    architecture character varying(50),
    serial_number character varying(100),
    firmware_type character varying(50),
    location character varying(255),
    notes text,
    tags json,
    access_method public.routeraccessmethod NOT NULL,
    jump_host_id uuid,
    nas_device_id uuid,
    network_device_id uuid,
    status public.routerstatus NOT NULL,
    last_seen_at timestamp with time zone,
    last_config_sync_at timestamp with time zone,
    last_config_change_at timestamp with time zone,
    reseller_id uuid,
    organization_id uuid,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: scheduled_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduled_tasks (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    task_name character varying(200) NOT NULL,
    schedule_type public.scheduletype NOT NULL,
    interval_seconds integer NOT NULL,
    args_json json,
    kwargs_json json,
    enabled boolean NOT NULL,
    last_run_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: service_buildings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_buildings (
    id uuid NOT NULL,
    code character varying(60),
    name character varying(200) NOT NULL,
    clli character varying(20),
    latitude double precision,
    longitude double precision,
    geom public.geometry(Point,4326),
    boundary_geom public.geometry(Polygon,4326),
    street character varying(200),
    city character varying(100),
    state character varying(60),
    zip_code character varying(20),
    work_order character varying(100),
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: service_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_orders (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    subscription_id uuid,
    status public.serviceorderstatus NOT NULL,
    order_type public.serviceordertype,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: service_qualifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_qualifications (
    id uuid NOT NULL,
    coverage_area_id uuid,
    address_id uuid,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    geom public.geometry(Point,4326),
    requested_tech character varying(60),
    status public.qualificationstatus NOT NULL,
    buildout_status public.buildoutstatus,
    estimated_install_window character varying(120),
    reasons json,
    metadata json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: service_state_transitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_state_transitions (
    id uuid NOT NULL,
    service_order_id uuid NOT NULL,
    from_state public.servicestate,
    to_state public.servicestate NOT NULL,
    reason character varying(200),
    changed_by character varying(120),
    changed_at timestamp with time zone NOT NULL
);


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sessions (
    id uuid NOT NULL,
    subscriber_id uuid,
    system_user_id uuid,
    status public.sessionstatus NOT NULL,
    token_hash character varying(255) NOT NULL,
    previous_token_hash character varying(255),
    token_rotated_at timestamp with time zone,
    ip_address character varying(64),
    user_agent character varying(512),
    created_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT ck_sessions_exactly_one_principal CHECK (((subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)))
);


--
-- Name: sla_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sla_profiles (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    uptime_percent numeric(5,2),
    response_time_hours integer,
    resolution_time_hours integer,
    credit_percent numeric(5,2),
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: snmp_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snmp_credentials (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    version public.snmpversion NOT NULL,
    community_hash character varying(255),
    username character varying(120),
    auth_protocol public.snmpauthprotocol NOT NULL,
    auth_secret_hash character varying(255),
    priv_protocol public.snmpprivprotocol NOT NULL,
    priv_secret_hash character varying(255),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: snmp_oids; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snmp_oids (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    oid character varying(120) NOT NULL,
    unit character varying(40),
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: snmp_pollers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snmp_pollers (
    id uuid NOT NULL,
    target_id uuid NOT NULL,
    oid_id uuid NOT NULL,
    poll_interval_sec integer NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: snmp_readings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snmp_readings (
    id uuid NOT NULL,
    poller_id uuid NOT NULL,
    value integer NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: snmp_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snmp_targets (
    id uuid NOT NULL,
    device_id uuid,
    hostname character varying(160),
    mgmt_ip character varying(64),
    port integer NOT NULL,
    credential_id uuid NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: speed_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.speed_profiles (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    direction public.speedprofiledirection NOT NULL,
    speed_kbps integer NOT NULL,
    speed_type public.speedprofiletype NOT NULL,
    use_prefix_suffix boolean NOT NULL,
    is_default boolean NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: speed_test_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.speed_test_results (
    id uuid NOT NULL,
    subscriber_id uuid,
    subscription_id uuid,
    network_device_id uuid,
    pop_site_id uuid,
    source public.speedtestsource NOT NULL,
    target_label character varying(160),
    provider character varying(120),
    server_name character varying(160),
    external_ip character varying(64),
    user_agent character varying(500),
    download_mbps double precision NOT NULL,
    upload_mbps double precision NOT NULL,
    latency_ms double precision,
    jitter_ms double precision,
    packet_loss_pct double precision,
    tested_at timestamp with time zone NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: splitter_port_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.splitter_port_assignments (
    id uuid NOT NULL,
    splitter_port_id uuid NOT NULL,
    subscriber_id uuid,
    subscription_id uuid,
    service_address_id uuid,
    assigned_at timestamp with time zone,
    active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: splitter_ports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.splitter_ports (
    id uuid NOT NULL,
    splitter_id uuid NOT NULL,
    port_number integer NOT NULL,
    port_type public.splitterporttype NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: splitters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.splitters (
    id uuid NOT NULL,
    fdh_id uuid,
    name character varying(160) NOT NULL,
    splitter_ratio character varying(40),
    input_ports integer NOT NULL,
    output_ports integer NOT NULL,
    zone_id uuid,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: splynx_archived_quote_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.splynx_archived_quote_items (
    id uuid NOT NULL,
    splynx_item_id integer,
    quote_id uuid NOT NULL,
    description text,
    quantity numeric(10,2) NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    amount numeric(12,2) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: splynx_archived_quotes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.splynx_archived_quotes (
    id uuid NOT NULL,
    splynx_quote_id integer NOT NULL,
    subscriber_id uuid,
    quote_number character varying(60),
    status character varying(40) NOT NULL,
    currency character varying(3) NOT NULL,
    subtotal numeric(12,2) NOT NULL,
    tax_total numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    valid_until timestamp with time zone,
    memo text,
    splynx_metadata jsonb,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: splynx_archived_ticket_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.splynx_archived_ticket_messages (
    id uuid NOT NULL,
    splynx_message_id integer NOT NULL,
    ticket_id uuid NOT NULL,
    sender_type character varying(20) NOT NULL,
    sender_name character varying(160),
    body text,
    is_internal boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: splynx_archived_tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.splynx_archived_tickets (
    id uuid NOT NULL,
    splynx_ticket_id integer NOT NULL,
    subscriber_id uuid,
    subject character varying(255) NOT NULL,
    status character varying(40) NOT NULL,
    priority character varying(20) NOT NULL,
    assigned_to character varying(160),
    created_by character varying(160),
    body text,
    splynx_metadata jsonb,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: splynx_id_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.splynx_id_mappings (
    id uuid NOT NULL,
    entity_type public.splynxentitytype NOT NULL,
    splynx_id integer NOT NULL,
    dotmac_id uuid NOT NULL,
    migrated_at timestamp with time zone NOT NULL,
    metadata json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: stored_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stored_files (
    id uuid NOT NULL,
    owner_subscriber_id uuid,
    entity_type character varying(100) NOT NULL,
    entity_id character varying(100) NOT NULL,
    original_filename character varying(255) NOT NULL,
    storage_key_or_relative_path character varying(1024) NOT NULL,
    legacy_local_path character varying(1024),
    file_size integer NOT NULL,
    content_type character varying(255),
    checksum character varying(64),
    storage_provider character varying(20) NOT NULL,
    uploaded_by uuid,
    uploaded_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    is_deleted boolean NOT NULL
);


--
-- Name: subscriber_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriber_channels (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    channel_type public.channeltype NOT NULL,
    address character varying(255) NOT NULL,
    label character varying(60),
    is_primary boolean NOT NULL,
    is_verified boolean NOT NULL,
    verified_at timestamp with time zone,
    metadata json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: subscriber_custom_fields; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriber_custom_fields (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    key character varying(120) NOT NULL,
    value_type public.settingvaluetype NOT NULL,
    value_text text,
    value_json json,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: subscriber_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriber_permissions (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    permission_id uuid NOT NULL,
    granted_at timestamp with time zone NOT NULL,
    granted_by_subscriber_id uuid
);


--
-- Name: subscriber_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriber_roles (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    role_id uuid NOT NULL,
    assigned_at timestamp with time zone NOT NULL
);


--
-- Name: subscribers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscribers (
    id uuid NOT NULL,
    first_name character varying(80) NOT NULL,
    last_name character varying(80) NOT NULL,
    display_name character varying(120),
    avatar_url character varying(512),
    company_name character varying(160),
    legal_name character varying(200),
    tax_id character varying(80),
    domain character varying(120),
    website character varying(255),
    email character varying(255) NOT NULL,
    email_verified boolean NOT NULL,
    phone character varying(40),
    date_of_birth date,
    gender public.gender NOT NULL,
    preferred_contact_method public.contactmethod,
    locale character varying(16),
    timezone character varying(64),
    address_line1 character varying(120),
    address_line2 character varying(120),
    city character varying(80),
    region character varying(80),
    postal_code character varying(20),
    country_code character varying(2),
    pop_site_id uuid,
    subscriber_number character varying(80),
    account_number character varying(80),
    account_start_date timestamp with time zone,
    status public.subscriberstatus NOT NULL,
    user_type public.usertype NOT NULL,
    is_active boolean NOT NULL,
    marketing_opt_in boolean NOT NULL,
    reseller_id uuid,
    tax_rate_id uuid,
    billing_enabled boolean NOT NULL,
    captive_redirect_enabled boolean NOT NULL,
    billing_name character varying(160),
    billing_address_line1 character varying(160),
    billing_address_line2 character varying(120),
    billing_city character varying(80),
    billing_region character varying(80),
    billing_postal_code character varying(20),
    billing_country_code character varying(2),
    payment_method character varying(80),
    deposit numeric(12,2),
    billing_mode public.billingmode NOT NULL,
    billing_day integer,
    payment_due_days integer,
    grace_period_days integer,
    min_balance numeric(12,2),
    prepaid_low_balance_at timestamp with time zone,
    prepaid_deactivation_at timestamp with time zone,
    mrr_total numeric(12,2),
    notes text,
    splynx_customer_id integer,
    metadata json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: subscription_add_ons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscription_add_ons (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    add_on_id uuid NOT NULL,
    quantity integer NOT NULL,
    start_at timestamp with time zone,
    end_at timestamp with time zone
);


--
-- Name: subscription_change_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscription_change_requests (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    current_offer_id uuid NOT NULL,
    requested_offer_id uuid NOT NULL,
    status public.subscriptionchangestatus NOT NULL,
    effective_date date NOT NULL,
    requested_by_subscriber_id uuid,
    reviewed_by_subscriber_id uuid,
    requested_at timestamp with time zone NOT NULL,
    reviewed_at timestamp with time zone,
    applied_at timestamp with time zone,
    notes text,
    rejection_reason text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: subscription_engine_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscription_engine_settings (
    id uuid NOT NULL,
    engine_id uuid NOT NULL,
    key character varying(120) NOT NULL,
    value_type public.settingvaluetype NOT NULL,
    value_text text,
    value_json json,
    is_secret boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: subscription_engines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscription_engines (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    code character varying(60) NOT NULL,
    description text,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: subscription_lifecycle_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscription_lifecycle_events (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    event_type public.lifecycleeventtype NOT NULL,
    from_status public.subscriptionstatus,
    to_status public.subscriptionstatus,
    reason character varying(200),
    notes text,
    metadata json,
    actor character varying(120),
    created_at timestamp with time zone NOT NULL
);


--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriptions (
    id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    offer_id uuid NOT NULL,
    offer_version_id uuid,
    service_address_id uuid,
    provisioning_nas_device_id uuid,
    radius_profile_id uuid,
    status public.subscriptionstatus NOT NULL,
    billing_mode public.billingmode NOT NULL,
    contract_term public.contractterm NOT NULL,
    start_at timestamp with time zone,
    end_at timestamp with time zone,
    next_billing_at timestamp with time zone,
    canceled_at timestamp with time zone,
    cancel_reason character varying(200),
    splynx_service_id integer,
    router_id integer,
    service_description text,
    quantity integer,
    unit character varying(40),
    unit_price numeric(12,2),
    discount boolean NOT NULL,
    discount_value numeric(12,2),
    discount_type public.discounttype,
    discount_start_at timestamp with time zone,
    discount_end_at timestamp with time zone,
    discount_description character varying(512),
    service_status_raw character varying(40),
    login character varying(120),
    ipv4_address character varying(64),
    ipv6_address character varying(128),
    mac_address character varying(64),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: support_ticket_assignees; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_ticket_assignees (
    ticket_id uuid NOT NULL,
    person_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: support_ticket_comments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_ticket_comments (
    id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    author_person_id uuid,
    body text NOT NULL,
    is_internal boolean NOT NULL,
    attachments json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: support_ticket_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_ticket_links (
    id uuid NOT NULL,
    from_ticket_id uuid NOT NULL,
    to_ticket_id uuid NOT NULL,
    link_type character varying(80) NOT NULL,
    created_by_person_id uuid,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: support_ticket_merges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_ticket_merges (
    source_ticket_id uuid NOT NULL,
    target_ticket_id uuid NOT NULL,
    reason text,
    merged_by_person_id uuid,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: support_ticket_sla_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_ticket_sla_events (
    id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    event_type character varying(80) NOT NULL,
    expected_at timestamp with time zone,
    actual_at timestamp with time zone,
    metadata json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: support_tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_tickets (
    id uuid NOT NULL,
    subscriber_id uuid,
    customer_account_id uuid,
    lead_id uuid,
    customer_person_id uuid,
    created_by_person_id uuid,
    assigned_to_person_id uuid,
    technician_person_id uuid,
    ticket_manager_person_id uuid,
    site_coordinator_person_id uuid,
    service_team_id uuid,
    number character varying(50),
    title character varying(255) NOT NULL,
    description text,
    region character varying(80),
    status public.ticketstatus NOT NULL,
    priority public.ticketpriority NOT NULL,
    ticket_type character varying(80),
    channel public.ticketchannel NOT NULL,
    tags json,
    metadata json,
    attachments json,
    due_at timestamp with time zone,
    resolved_at timestamp with time zone,
    closed_at timestamp with time zone,
    merged_into_ticket_id uuid,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: survey_responses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.survey_responses (
    id uuid NOT NULL,
    survey_id uuid NOT NULL,
    work_order_id uuid,
    ticket_id uuid,
    responses json,
    rating integer,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: surveys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.surveys (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    description text,
    questions json,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: system_user_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_user_permissions (
    id uuid NOT NULL,
    system_user_id uuid NOT NULL,
    permission_id uuid NOT NULL,
    granted_at timestamp with time zone NOT NULL,
    granted_by_system_user_id uuid
);


--
-- Name: system_user_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_user_roles (
    id uuid NOT NULL,
    system_user_id uuid NOT NULL,
    role_id uuid NOT NULL,
    assigned_at timestamp with time zone NOT NULL
);


--
-- Name: system_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_users (
    id uuid NOT NULL,
    first_name character varying(80) NOT NULL,
    last_name character varying(80) NOT NULL,
    display_name character varying(120),
    email character varying(255) NOT NULL,
    phone character varying(40),
    user_type public.usertype NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: table_column_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.table_column_config (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    table_key character varying(120) NOT NULL,
    column_key character varying(120) NOT NULL,
    display_order integer NOT NULL,
    is_visible boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: table_column_default_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.table_column_default_config (
    id uuid NOT NULL,
    table_key character varying(120) NOT NULL,
    column_key character varying(120) NOT NULL,
    display_order integer NOT NULL,
    is_visible boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tax_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_rates (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    code character varying(40),
    rate numeric(6,4) NOT NULL,
    is_active boolean NOT NULL,
    description text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tr069_acs_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tr069_acs_servers (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    cwmp_url character varying(255),
    cwmp_username character varying(120),
    cwmp_password character varying(255),
    connection_request_username character varying(120),
    connection_request_password character varying(255),
    base_url character varying(255) NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tr069_cpe_devices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tr069_cpe_devices (
    id uuid NOT NULL,
    acs_server_id uuid NOT NULL,
    ont_unit_id uuid,
    cpe_device_id uuid,
    serial_number character varying(120),
    oui character varying(8),
    product_class character varying(120),
    connection_request_url character varying(255),
    last_inform_at timestamp with time zone,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tr069_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tr069_jobs (
    id uuid NOT NULL,
    device_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    command character varying(160) NOT NULL,
    payload json,
    status public.tr069jobstatus NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    error text,
    retry_count integer NOT NULL,
    max_retries integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tr069_parameter_maps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tr069_parameter_maps (
    id uuid NOT NULL,
    capability_id uuid NOT NULL,
    canonical_name character varying(200) NOT NULL,
    tr069_path character varying(500) NOT NULL,
    writable boolean NOT NULL,
    value_type character varying(40),
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tr069_parameters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tr069_parameters (
    id uuid NOT NULL,
    device_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    value text,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tr069_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tr069_sessions (
    id uuid NOT NULL,
    device_id uuid NOT NULL,
    event_type public.tr069event NOT NULL,
    request_id character varying(120),
    inform_payload json,
    started_at timestamp with time zone NOT NULL,
    ended_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: usage_allowances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_allowances (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    included_gb integer,
    overage_rate numeric(10,2),
    overage_cap_gb integer,
    throttle_rate_mbps integer,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: usage_charges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_charges (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    invoice_line_id uuid,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    total_gb numeric(12,4) NOT NULL,
    included_gb numeric(12,4) NOT NULL,
    billable_gb numeric(12,4) NOT NULL,
    unit_price numeric(10,4) NOT NULL,
    amount numeric(12,2) NOT NULL,
    currency character varying(3) NOT NULL,
    status public.usagechargestatus NOT NULL,
    notes text,
    rated_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: usage_rating_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rating_runs (
    id uuid NOT NULL,
    run_at timestamp with time zone NOT NULL,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    status public.usageratingrunstatus NOT NULL,
    subscriptions_scanned integer NOT NULL,
    charges_created integer NOT NULL,
    skipped integer NOT NULL,
    error text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: usage_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_records (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    quota_bucket_id uuid,
    source public.usagesource NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    input_gb numeric(12,4) NOT NULL,
    output_gb numeric(12,4) NOT NULL,
    total_gb numeric(12,4) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: user_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_credentials (
    id uuid NOT NULL,
    subscriber_id uuid,
    system_user_id uuid,
    provider public.authprovider NOT NULL,
    username character varying(150),
    password_hash character varying(255),
    radius_server_id uuid,
    must_change_password boolean NOT NULL,
    password_updated_at timestamp with time zone,
    failed_login_attempts integer NOT NULL,
    locked_until timestamp with time zone,
    last_login_at timestamp with time zone,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_user_credentials_exactly_one_principal CHECK (((subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL))),
    CONSTRAINT ck_user_credentials_local_requires_username_password CHECK (((provider <> 'local'::public.authprovider) OR ((username IS NOT NULL) AND (password_hash IS NOT NULL))))
);


--
-- Name: vendor_model_capabilities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_model_capabilities (
    id uuid NOT NULL,
    vendor character varying(120) NOT NULL,
    model character varying(120) NOT NULL,
    firmware_pattern character varying(200),
    tr069_root character varying(200),
    supported_features json,
    max_wan_services integer NOT NULL,
    max_lan_ports integer NOT NULL,
    max_ssids integer NOT NULL,
    supports_vlan_tagging boolean NOT NULL,
    supports_qinq boolean NOT NULL,
    supports_ipv6 boolean NOT NULL,
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: vlans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vlans (
    id uuid NOT NULL,
    region_id uuid NOT NULL,
    tag integer NOT NULL,
    name character varying(120),
    description text,
    purpose public.vlanpurpose,
    dhcp_snooping boolean NOT NULL,
    olt_device_id uuid,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: webhook_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_deliveries (
    id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    endpoint_id uuid NOT NULL,
    event_type public.webhookeventtype NOT NULL,
    status public.webhookdeliverystatus NOT NULL,
    attempt_count integer NOT NULL,
    last_attempt_at timestamp with time zone,
    delivered_at timestamp with time zone,
    response_status integer,
    error text,
    payload json,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: webhook_endpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_endpoints (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    url character varying(500) NOT NULL,
    connector_config_id uuid,
    secret character varying(255),
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: webhook_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_subscriptions (
    id uuid NOT NULL,
    endpoint_id uuid NOT NULL,
    event_type public.webhookeventtype NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: wireguard_connection_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wireguard_connection_logs (
    id uuid NOT NULL,
    peer_id uuid NOT NULL,
    connected_at timestamp with time zone NOT NULL,
    disconnected_at timestamp with time zone,
    endpoint_ip character varying(64),
    peer_address character varying(64),
    rx_bytes bigint NOT NULL,
    tx_bytes bigint NOT NULL,
    disconnect_reason character varying(255)
);


--
-- Name: wireguard_peers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wireguard_peers (
    id uuid NOT NULL,
    server_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    description text,
    public_key character varying(64) NOT NULL,
    private_key text,
    preshared_key text,
    allowed_ips json,
    peer_address character varying(64),
    peer_address_v6 character varying(64),
    persistent_keepalive integer NOT NULL,
    status public.wireguardpeerstatus NOT NULL,
    provision_token_hash character varying(128),
    provision_token_expires_at timestamp with time zone,
    last_handshake_at timestamp with time zone,
    endpoint_ip character varying(64),
    rx_bytes bigint NOT NULL,
    tx_bytes bigint NOT NULL,
    metadata json,
    notes text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: wireguard_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wireguard_servers (
    id uuid NOT NULL,
    name character varying(160) NOT NULL,
    description text,
    interface_name character varying(32) NOT NULL,
    listen_port integer NOT NULL,
    private_key text,
    public_key character varying(64),
    public_host character varying(255),
    public_port integer,
    vpn_address character varying(64) NOT NULL,
    vpn_address_v6 character varying(64),
    mtu integer NOT NULL,
    dns_servers json,
    is_active boolean NOT NULL,
    metadata json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: access_credentials access_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_credentials
    ADD CONSTRAINT access_credentials_pkey PRIMARY KEY (id);


--
-- Name: add_on_prices add_on_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.add_on_prices
    ADD CONSTRAINT add_on_prices_pkey PRIMARY KEY (id);


--
-- Name: add_ons add_ons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.add_ons
    ADD CONSTRAINT add_ons_pkey PRIMARY KEY (id);


--
-- Name: addresses addresses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.addresses
    ADD CONSTRAINT addresses_pkey PRIMARY KEY (id);


--
-- Name: alert_events alert_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_events
    ADD CONSTRAINT alert_events_pkey PRIMARY KEY (id);


--
-- Name: alert_notification_logs alert_notification_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_logs
    ADD CONSTRAINT alert_notification_logs_pkey PRIMARY KEY (id);


--
-- Name: alert_notification_policies alert_notification_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policies
    ADD CONSTRAINT alert_notification_policies_pkey PRIMARY KEY (id);


--
-- Name: alert_notification_policy_steps alert_notification_policy_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policy_steps
    ADD CONSTRAINT alert_notification_policy_steps_pkey PRIMARY KEY (id);


--
-- Name: alert_rules alert_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_rules
    ADD CONSTRAINT alert_rules_pkey PRIMARY KEY (id);


--
-- Name: alerts alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_pkey PRIMARY KEY (id);


--
-- Name: api_keys api_keys_key_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_key_hash_key UNIQUE (key_hash);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (id);


--
-- Name: bandwidth_samples bandwidth_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bandwidth_samples
    ADD CONSTRAINT bandwidth_samples_pkey PRIMARY KEY (id);


--
-- Name: bank_accounts bank_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_accounts
    ADD CONSTRAINT bank_accounts_pkey PRIMARY KEY (id);


--
-- Name: bank_reconciliation_items bank_reconciliation_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_items
    ADD CONSTRAINT bank_reconciliation_items_pkey PRIMARY KEY (id);


--
-- Name: bank_reconciliation_runs bank_reconciliation_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_runs
    ADD CONSTRAINT bank_reconciliation_runs_pkey PRIMARY KEY (id);


--
-- Name: billing_run_schedules billing_run_schedules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_run_schedules
    ADD CONSTRAINT billing_run_schedules_pkey PRIMARY KEY (id);


--
-- Name: billing_runs billing_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_runs
    ADD CONSTRAINT billing_runs_pkey PRIMARY KEY (id);


--
-- Name: buildout_milestones buildout_milestones_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_milestones
    ADD CONSTRAINT buildout_milestones_pkey PRIMARY KEY (id);


--
-- Name: buildout_projects buildout_projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_projects
    ADD CONSTRAINT buildout_projects_pkey PRIMARY KEY (id);


--
-- Name: buildout_requests buildout_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_requests
    ADD CONSTRAINT buildout_requests_pkey PRIMARY KEY (id);


--
-- Name: buildout_updates buildout_updates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_updates
    ADD CONSTRAINT buildout_updates_pkey PRIMARY KEY (id);


--
-- Name: catalog_offers catalog_offers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_offers
    ADD CONSTRAINT catalog_offers_pkey PRIMARY KEY (id);


--
-- Name: collection_accounts collection_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_accounts
    ADD CONSTRAINT collection_accounts_pkey PRIMARY KEY (id);


--
-- Name: communication_logs communication_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.communication_logs
    ADD CONSTRAINT communication_logs_pkey PRIMARY KEY (id);


--
-- Name: connector_configs connector_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_configs
    ADD CONSTRAINT connector_configs_pkey PRIMARY KEY (id);


--
-- Name: contract_signatures contract_signatures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contract_signatures
    ADD CONSTRAINT contract_signatures_pkey PRIMARY KEY (id);


--
-- Name: coverage_areas coverage_areas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coverage_areas
    ADD CONSTRAINT coverage_areas_pkey PRIMARY KEY (id);


--
-- Name: cpe_devices cpe_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cpe_devices
    ADD CONSTRAINT cpe_devices_pkey PRIMARY KEY (id);


--
-- Name: credit_note_applications credit_note_applications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_note_applications
    ADD CONSTRAINT credit_note_applications_pkey PRIMARY KEY (id);


--
-- Name: credit_note_lines credit_note_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_note_lines
    ADD CONSTRAINT credit_note_lines_pkey PRIMARY KEY (id);


--
-- Name: credit_notes credit_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_notes
    ADD CONSTRAINT credit_notes_pkey PRIMARY KEY (id);


--
-- Name: customer_notification_events customer_notification_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_notification_events
    ADD CONSTRAINT customer_notification_events_pkey PRIMARY KEY (id);


--
-- Name: device_interfaces device_interfaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_interfaces
    ADD CONSTRAINT device_interfaces_pkey PRIMARY KEY (id);


--
-- Name: device_metrics device_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_metrics
    ADD CONSTRAINT device_metrics_pkey PRIMARY KEY (id);


--
-- Name: dns_threat_events dns_threat_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dns_threat_events
    ADD CONSTRAINT dns_threat_events_pkey PRIMARY KEY (id);


--
-- Name: domain_settings domain_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_settings
    ADD CONSTRAINT domain_settings_pkey PRIMARY KEY (id);


--
-- Name: dunning_action_logs dunning_action_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dunning_action_logs
    ADD CONSTRAINT dunning_action_logs_pkey PRIMARY KEY (id);


--
-- Name: dunning_cases dunning_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dunning_cases
    ADD CONSTRAINT dunning_cases_pkey PRIMARY KEY (id);


--
-- Name: enforcement_locks enforcement_locks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enforcement_locks
    ADD CONSTRAINT enforcement_locks_pkey PRIMARY KEY (id);


--
-- Name: eta_updates eta_updates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eta_updates
    ADD CONSTRAINT eta_updates_pkey PRIMARY KEY (id);


--
-- Name: event_store event_store_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_store
    ADD CONSTRAINT event_store_pkey PRIMARY KEY (id);


--
-- Name: external_references external_references_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_references
    ADD CONSTRAINT external_references_pkey PRIMARY KEY (id);


--
-- Name: fdh_cabinets fdh_cabinets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fdh_cabinets
    ADD CONSTRAINT fdh_cabinets_pkey PRIMARY KEY (id);


--
-- Name: fiber_access_points fiber_access_points_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_access_points
    ADD CONSTRAINT fiber_access_points_code_key UNIQUE (code);


--
-- Name: fiber_access_points fiber_access_points_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_access_points
    ADD CONSTRAINT fiber_access_points_pkey PRIMARY KEY (id);


--
-- Name: fiber_change_requests fiber_change_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_change_requests
    ADD CONSTRAINT fiber_change_requests_pkey PRIMARY KEY (id);


--
-- Name: fiber_segments fiber_segments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_segments
    ADD CONSTRAINT fiber_segments_pkey PRIMARY KEY (id);


--
-- Name: fiber_splice_closures fiber_splice_closures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splice_closures
    ADD CONSTRAINT fiber_splice_closures_pkey PRIMARY KEY (id);


--
-- Name: fiber_splice_trays fiber_splice_trays_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splice_trays
    ADD CONSTRAINT fiber_splice_trays_pkey PRIMARY KEY (id);


--
-- Name: fiber_splices fiber_splices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splices
    ADD CONSTRAINT fiber_splices_pkey PRIMARY KEY (id);


--
-- Name: fiber_strands fiber_strands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_strands
    ADD CONSTRAINT fiber_strands_pkey PRIMARY KEY (id);


--
-- Name: fiber_termination_points fiber_termination_points_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_termination_points
    ADD CONSTRAINT fiber_termination_points_pkey PRIMARY KEY (id);


--
-- Name: fup_policies fup_policies_offer_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_policies
    ADD CONSTRAINT fup_policies_offer_id_key UNIQUE (offer_id);


--
-- Name: fup_policies fup_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_policies
    ADD CONSTRAINT fup_policies_pkey PRIMARY KEY (id);


--
-- Name: fup_rules fup_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_rules
    ADD CONSTRAINT fup_rules_pkey PRIMARY KEY (id);


--
-- Name: fup_states fup_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_states
    ADD CONSTRAINT fup_states_pkey PRIMARY KEY (id);


--
-- Name: geo_areas geo_areas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geo_areas
    ADD CONSTRAINT geo_areas_pkey PRIMARY KEY (id);


--
-- Name: geo_layers geo_layers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geo_layers
    ADD CONSTRAINT geo_layers_pkey PRIMARY KEY (id);


--
-- Name: geo_locations geo_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geo_locations
    ADD CONSTRAINT geo_locations_pkey PRIMARY KEY (id);


--
-- Name: install_appointments install_appointments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.install_appointments
    ADD CONSTRAINT install_appointments_pkey PRIMARY KEY (id);


--
-- Name: integration_connectors integration_connectors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_connectors
    ADD CONSTRAINT integration_connectors_pkey PRIMARY KEY (id);


--
-- Name: integration_hook_executions integration_hook_executions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_hook_executions
    ADD CONSTRAINT integration_hook_executions_pkey PRIMARY KEY (id);


--
-- Name: integration_hooks integration_hooks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_hooks
    ADD CONSTRAINT integration_hooks_pkey PRIMARY KEY (id);


--
-- Name: integration_jobs integration_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_jobs
    ADD CONSTRAINT integration_jobs_pkey PRIMARY KEY (id);


--
-- Name: integration_runs integration_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_runs
    ADD CONSTRAINT integration_runs_pkey PRIMARY KEY (id);


--
-- Name: integration_targets integration_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_targets
    ADD CONSTRAINT integration_targets_pkey PRIMARY KEY (id);


--
-- Name: invoice_lines invoice_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_pkey PRIMARY KEY (id);


--
-- Name: invoice_pdf_exports invoice_pdf_exports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_pdf_exports
    ADD CONSTRAINT invoice_pdf_exports_pkey PRIMARY KEY (id);


--
-- Name: invoices invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_pkey PRIMARY KEY (id);


--
-- Name: ip_assignments ip_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT ip_assignments_pkey PRIMARY KEY (id);


--
-- Name: ip_blocks ip_blocks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_blocks
    ADD CONSTRAINT ip_blocks_pkey PRIMARY KEY (id);


--
-- Name: ip_pools ip_pools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_pools
    ADD CONSTRAINT ip_pools_pkey PRIMARY KEY (id);


--
-- Name: ipv4_addresses ipv4_addresses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ipv4_addresses
    ADD CONSTRAINT ipv4_addresses_pkey PRIMARY KEY (id);


--
-- Name: ipv6_addresses ipv6_addresses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ipv6_addresses
    ADD CONSTRAINT ipv6_addresses_pkey PRIMARY KEY (id);


--
-- Name: jump_hosts jump_hosts_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jump_hosts
    ADD CONSTRAINT jump_hosts_name_key UNIQUE (name);


--
-- Name: jump_hosts jump_hosts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jump_hosts
    ADD CONSTRAINT jump_hosts_pkey PRIMARY KEY (id);


--
-- Name: kpi_aggregates kpi_aggregates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kpi_aggregates
    ADD CONSTRAINT kpi_aggregates_pkey PRIMARY KEY (id);


--
-- Name: kpi_configs kpi_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kpi_configs
    ADD CONSTRAINT kpi_configs_pkey PRIMARY KEY (id);


--
-- Name: ledger_entries ledger_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ledger_entries
    ADD CONSTRAINT ledger_entries_pkey PRIMARY KEY (id);


--
-- Name: legal_documents legal_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legal_documents
    ADD CONSTRAINT legal_documents_pkey PRIMARY KEY (id);


--
-- Name: legal_documents legal_documents_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legal_documents
    ADD CONSTRAINT legal_documents_slug_key UNIQUE (slug);


--
-- Name: mfa_methods mfa_methods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_methods
    ADD CONSTRAINT mfa_methods_pkey PRIMARY KEY (id);


--
-- Name: mrr_snapshots mrr_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mrr_snapshots
    ADD CONSTRAINT mrr_snapshots_pkey PRIMARY KEY (id);


--
-- Name: nas_config_backups nas_config_backups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_config_backups
    ADD CONSTRAINT nas_config_backups_pkey PRIMARY KEY (id);


--
-- Name: nas_connection_rules nas_connection_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_connection_rules
    ADD CONSTRAINT nas_connection_rules_pkey PRIMARY KEY (id);


--
-- Name: nas_devices nas_devices_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_devices
    ADD CONSTRAINT nas_devices_code_key UNIQUE (code);


--
-- Name: nas_devices nas_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_devices
    ADD CONSTRAINT nas_devices_pkey PRIMARY KEY (id);


--
-- Name: network_device_bandwidth_graph_sources network_device_bandwidth_graph_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_bandwidth_graph_sources
    ADD CONSTRAINT network_device_bandwidth_graph_sources_pkey PRIMARY KEY (id);


--
-- Name: network_device_bandwidth_graphs network_device_bandwidth_graphs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_bandwidth_graphs
    ADD CONSTRAINT network_device_bandwidth_graphs_pkey PRIMARY KEY (id);


--
-- Name: network_device_bandwidth_graphs network_device_bandwidth_graphs_public_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_bandwidth_graphs
    ADD CONSTRAINT network_device_bandwidth_graphs_public_token_key UNIQUE (public_token);


--
-- Name: network_device_snmp_oids network_device_snmp_oids_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_snmp_oids
    ADD CONSTRAINT network_device_snmp_oids_pkey PRIMARY KEY (id);


--
-- Name: network_devices network_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_devices
    ADD CONSTRAINT network_devices_pkey PRIMARY KEY (id);


--
-- Name: network_operations network_operations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_operations
    ADD CONSTRAINT network_operations_pkey PRIMARY KEY (id);


--
-- Name: network_topology_links network_topology_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_topology_links
    ADD CONSTRAINT network_topology_links_pkey PRIMARY KEY (id);


--
-- Name: network_zones network_zones_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_zones
    ADD CONSTRAINT network_zones_pkey PRIMARY KEY (id);


--
-- Name: notification_deliveries notification_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_pkey PRIMARY KEY (id);


--
-- Name: notification_templates notification_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_templates
    ADD CONSTRAINT notification_templates_pkey PRIMARY KEY (id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: oauth_tokens oauth_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT oauth_tokens_pkey PRIMARY KEY (id);


--
-- Name: offer_add_ons offer_add_ons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_add_ons
    ADD CONSTRAINT offer_add_ons_pkey PRIMARY KEY (id);


--
-- Name: offer_billing_mode_availability offer_billing_mode_availability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_billing_mode_availability
    ADD CONSTRAINT offer_billing_mode_availability_pkey PRIMARY KEY (id);


--
-- Name: offer_category_availability offer_category_availability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_category_availability
    ADD CONSTRAINT offer_category_availability_pkey PRIMARY KEY (id);


--
-- Name: offer_location_availability offer_location_availability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_location_availability
    ADD CONSTRAINT offer_location_availability_pkey PRIMARY KEY (id);


--
-- Name: offer_prices offer_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_prices
    ADD CONSTRAINT offer_prices_pkey PRIMARY KEY (id);


--
-- Name: offer_radius_profiles offer_radius_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_radius_profiles
    ADD CONSTRAINT offer_radius_profiles_pkey PRIMARY KEY (id);


--
-- Name: offer_reseller_availability offer_reseller_availability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_reseller_availability
    ADD CONSTRAINT offer_reseller_availability_pkey PRIMARY KEY (id);


--
-- Name: offer_version_prices offer_version_prices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_version_prices
    ADD CONSTRAINT offer_version_prices_pkey PRIMARY KEY (id);


--
-- Name: offer_versions offer_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_versions
    ADD CONSTRAINT offer_versions_pkey PRIMARY KEY (id);


--
-- Name: olt_autofind_candidates olt_autofind_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_autofind_candidates
    ADD CONSTRAINT olt_autofind_candidates_pkey PRIMARY KEY (id);


--
-- Name: olt_card_ports olt_card_ports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_card_ports
    ADD CONSTRAINT olt_card_ports_pkey PRIMARY KEY (id);


--
-- Name: olt_cards olt_cards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_cards
    ADD CONSTRAINT olt_cards_pkey PRIMARY KEY (id);


--
-- Name: olt_config_backups olt_config_backups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_config_backups
    ADD CONSTRAINT olt_config_backups_pkey PRIMARY KEY (id);


--
-- Name: olt_devices olt_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_devices
    ADD CONSTRAINT olt_devices_pkey PRIMARY KEY (id);


--
-- Name: olt_firmware_images olt_firmware_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_firmware_images
    ADD CONSTRAINT olt_firmware_images_pkey PRIMARY KEY (id);


--
-- Name: olt_power_units olt_power_units_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_power_units
    ADD CONSTRAINT olt_power_units_pkey PRIMARY KEY (id);


--
-- Name: olt_sfp_modules olt_sfp_modules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_sfp_modules
    ADD CONSTRAINT olt_sfp_modules_pkey PRIMARY KEY (id);


--
-- Name: olt_shelves olt_shelves_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_shelves
    ADD CONSTRAINT olt_shelves_pkey PRIMARY KEY (id);


--
-- Name: on_call_rotation_members on_call_rotation_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.on_call_rotation_members
    ADD CONSTRAINT on_call_rotation_members_pkey PRIMARY KEY (id);


--
-- Name: on_call_rotations on_call_rotations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.on_call_rotations
    ADD CONSTRAINT on_call_rotations_pkey PRIMARY KEY (id);


--
-- Name: ont_assignments ont_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_assignments
    ADD CONSTRAINT ont_assignments_pkey PRIMARY KEY (id);


--
-- Name: ont_firmware_images ont_firmware_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_firmware_images
    ADD CONSTRAINT ont_firmware_images_pkey PRIMARY KEY (id);


--
-- Name: ont_profile_wan_services ont_profile_wan_services_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_profile_wan_services
    ADD CONSTRAINT ont_profile_wan_services_pkey PRIMARY KEY (id);


--
-- Name: ont_provisioning_profiles ont_provisioning_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_provisioning_profiles
    ADD CONSTRAINT ont_provisioning_profiles_pkey PRIMARY KEY (id);


--
-- Name: ont_units ont_units_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_pkey PRIMARY KEY (id);


--
-- Name: onu_types onu_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onu_types
    ADD CONSTRAINT onu_types_pkey PRIMARY KEY (id);


--
-- Name: payment_allocations payment_allocations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocations
    ADD CONSTRAINT payment_allocations_pkey PRIMARY KEY (id);


--
-- Name: payment_arrangement_installments payment_arrangement_installments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_arrangement_installments
    ADD CONSTRAINT payment_arrangement_installments_pkey PRIMARY KEY (id);


--
-- Name: payment_arrangements payment_arrangements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_arrangements
    ADD CONSTRAINT payment_arrangements_pkey PRIMARY KEY (id);


--
-- Name: payment_channel_accounts payment_channel_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_channel_accounts
    ADD CONSTRAINT payment_channel_accounts_pkey PRIMARY KEY (id);


--
-- Name: payment_channels payment_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_channels
    ADD CONSTRAINT payment_channels_pkey PRIMARY KEY (id);


--
-- Name: payment_methods payment_methods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_methods
    ADD CONSTRAINT payment_methods_pkey PRIMARY KEY (id);


--
-- Name: payment_provider_events payment_provider_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_provider_events
    ADD CONSTRAINT payment_provider_events_pkey PRIMARY KEY (id);


--
-- Name: payment_providers payment_providers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_providers
    ADD CONSTRAINT payment_providers_pkey PRIMARY KEY (id);


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id);


--
-- Name: permissions permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id);


--
-- Name: policy_dunning_steps policy_dunning_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_dunning_steps
    ADD CONSTRAINT policy_dunning_steps_pkey PRIMARY KEY (id);


--
-- Name: policy_sets policy_sets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_sets
    ADD CONSTRAINT policy_sets_pkey PRIMARY KEY (id);


--
-- Name: pon_port_splitter_links pon_port_splitter_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pon_port_splitter_links
    ADD CONSTRAINT pon_port_splitter_links_pkey PRIMARY KEY (id);


--
-- Name: pon_ports pon_ports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pon_ports
    ADD CONSTRAINT pon_ports_pkey PRIMARY KEY (id);


--
-- Name: pop_site_contacts pop_site_contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pop_site_contacts
    ADD CONSTRAINT pop_site_contacts_pkey PRIMARY KEY (id);


--
-- Name: pop_sites pop_sites_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pop_sites
    ADD CONSTRAINT pop_sites_code_key UNIQUE (code);


--
-- Name: pop_sites pop_sites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pop_sites
    ADD CONSTRAINT pop_sites_pkey PRIMARY KEY (id);


--
-- Name: port_vlans port_vlans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.port_vlans
    ADD CONSTRAINT port_vlans_pkey PRIMARY KEY (id);


--
-- Name: portal_messages portal_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_messages
    ADD CONSTRAINT portal_messages_pkey PRIMARY KEY (id);


--
-- Name: portal_onboarding_states portal_onboarding_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_onboarding_states
    ADD CONSTRAINT portal_onboarding_states_pkey PRIMARY KEY (id);


--
-- Name: portal_onboarding_states portal_onboarding_states_subscriber_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_onboarding_states
    ADD CONSTRAINT portal_onboarding_states_subscriber_id_key UNIQUE (subscriber_id);


--
-- Name: ports ports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ports
    ADD CONSTRAINT ports_pkey PRIMARY KEY (id);


--
-- Name: provisioning_logs provisioning_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_logs
    ADD CONSTRAINT provisioning_logs_pkey PRIMARY KEY (id);


--
-- Name: provisioning_runs provisioning_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_runs
    ADD CONSTRAINT provisioning_runs_pkey PRIMARY KEY (id);


--
-- Name: provisioning_steps provisioning_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_steps
    ADD CONSTRAINT provisioning_steps_pkey PRIMARY KEY (id);


--
-- Name: provisioning_tasks provisioning_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_tasks
    ADD CONSTRAINT provisioning_tasks_pkey PRIMARY KEY (id);


--
-- Name: provisioning_templates provisioning_templates_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_templates
    ADD CONSTRAINT provisioning_templates_code_key UNIQUE (code);


--
-- Name: provisioning_templates provisioning_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_templates
    ADD CONSTRAINT provisioning_templates_pkey PRIMARY KEY (id);


--
-- Name: provisioning_workflows provisioning_workflows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_workflows
    ADD CONSTRAINT provisioning_workflows_pkey PRIMARY KEY (id);


--
-- Name: queue_mappings queue_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.queue_mappings
    ADD CONSTRAINT queue_mappings_pkey PRIMARY KEY (id);


--
-- Name: quota_buckets quota_buckets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quota_buckets
    ADD CONSTRAINT quota_buckets_pkey PRIMARY KEY (id);


--
-- Name: radius_accounting_sessions radius_accounting_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_accounting_sessions
    ADD CONSTRAINT radius_accounting_sessions_pkey PRIMARY KEY (id);


--
-- Name: radius_active_sessions radius_active_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_active_sessions
    ADD CONSTRAINT radius_active_sessions_pkey PRIMARY KEY (id);


--
-- Name: radius_attributes radius_attributes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_attributes
    ADD CONSTRAINT radius_attributes_pkey PRIMARY KEY (id);


--
-- Name: radius_auth_errors radius_auth_errors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_auth_errors
    ADD CONSTRAINT radius_auth_errors_pkey PRIMARY KEY (id);


--
-- Name: radius_clients radius_clients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_clients
    ADD CONSTRAINT radius_clients_pkey PRIMARY KEY (id);


--
-- Name: radius_profiles radius_profiles_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_profiles
    ADD CONSTRAINT radius_profiles_code_key UNIQUE (code);


--
-- Name: radius_profiles radius_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_profiles
    ADD CONSTRAINT radius_profiles_pkey PRIMARY KEY (id);


--
-- Name: radius_servers radius_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_servers
    ADD CONSTRAINT radius_servers_pkey PRIMARY KEY (id);


--
-- Name: radius_sync_jobs radius_sync_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_sync_jobs
    ADD CONSTRAINT radius_sync_jobs_pkey PRIMARY KEY (id);


--
-- Name: radius_sync_runs radius_sync_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_sync_runs
    ADD CONSTRAINT radius_sync_runs_pkey PRIMARY KEY (id);


--
-- Name: radius_users radius_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_users
    ADD CONSTRAINT radius_users_pkey PRIMARY KEY (id);


--
-- Name: region_zones region_zones_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.region_zones
    ADD CONSTRAINT region_zones_pkey PRIMARY KEY (id);


--
-- Name: reseller_users reseller_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reseller_users
    ADD CONSTRAINT reseller_users_pkey PRIMARY KEY (id);


--
-- Name: resellers resellers_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resellers
    ADD CONSTRAINT resellers_code_key UNIQUE (code);


--
-- Name: resellers resellers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resellers
    ADD CONSTRAINT resellers_pkey PRIMARY KEY (id);


--
-- Name: role_permissions role_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (id);


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);


--
-- Name: router_config_push_results router_config_push_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_push_results
    ADD CONSTRAINT router_config_push_results_pkey PRIMARY KEY (id);


--
-- Name: router_config_pushes router_config_pushes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_pushes
    ADD CONSTRAINT router_config_pushes_pkey PRIMARY KEY (id);


--
-- Name: router_config_snapshots router_config_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_snapshots
    ADD CONSTRAINT router_config_snapshots_pkey PRIMARY KEY (id);


--
-- Name: router_config_templates router_config_templates_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_templates
    ADD CONSTRAINT router_config_templates_name_key UNIQUE (name);


--
-- Name: router_config_templates router_config_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_templates
    ADD CONSTRAINT router_config_templates_pkey PRIMARY KEY (id);


--
-- Name: router_interfaces router_interfaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_interfaces
    ADD CONSTRAINT router_interfaces_pkey PRIMARY KEY (id);


--
-- Name: routers routers_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routers
    ADD CONSTRAINT routers_name_key UNIQUE (name);


--
-- Name: routers routers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routers
    ADD CONSTRAINT routers_pkey PRIMARY KEY (id);


--
-- Name: scheduled_tasks scheduled_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_tasks
    ADD CONSTRAINT scheduled_tasks_pkey PRIMARY KEY (id);


--
-- Name: service_buildings service_buildings_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_buildings
    ADD CONSTRAINT service_buildings_code_key UNIQUE (code);


--
-- Name: service_buildings service_buildings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_buildings
    ADD CONSTRAINT service_buildings_pkey PRIMARY KEY (id);


--
-- Name: service_orders service_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_orders
    ADD CONSTRAINT service_orders_pkey PRIMARY KEY (id);


--
-- Name: service_qualifications service_qualifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_qualifications
    ADD CONSTRAINT service_qualifications_pkey PRIMARY KEY (id);


--
-- Name: service_state_transitions service_state_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_state_transitions
    ADD CONSTRAINT service_state_transitions_pkey PRIMARY KEY (id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);


--
-- Name: sla_profiles sla_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_profiles
    ADD CONSTRAINT sla_profiles_pkey PRIMARY KEY (id);


--
-- Name: snmp_credentials snmp_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_credentials
    ADD CONSTRAINT snmp_credentials_pkey PRIMARY KEY (id);


--
-- Name: snmp_oids snmp_oids_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_oids
    ADD CONSTRAINT snmp_oids_pkey PRIMARY KEY (id);


--
-- Name: snmp_pollers snmp_pollers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_pollers
    ADD CONSTRAINT snmp_pollers_pkey PRIMARY KEY (id);


--
-- Name: snmp_readings snmp_readings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_readings
    ADD CONSTRAINT snmp_readings_pkey PRIMARY KEY (id);


--
-- Name: snmp_targets snmp_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_targets
    ADD CONSTRAINT snmp_targets_pkey PRIMARY KEY (id);


--
-- Name: speed_profiles speed_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.speed_profiles
    ADD CONSTRAINT speed_profiles_pkey PRIMARY KEY (id);


--
-- Name: speed_test_results speed_test_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.speed_test_results
    ADD CONSTRAINT speed_test_results_pkey PRIMARY KEY (id);


--
-- Name: splitter_port_assignments splitter_port_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_port_assignments
    ADD CONSTRAINT splitter_port_assignments_pkey PRIMARY KEY (id);


--
-- Name: splitter_ports splitter_ports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_ports
    ADD CONSTRAINT splitter_ports_pkey PRIMARY KEY (id);


--
-- Name: splitters splitters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitters
    ADD CONSTRAINT splitters_pkey PRIMARY KEY (id);


--
-- Name: splynx_archived_quote_items splynx_archived_quote_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_quote_items
    ADD CONSTRAINT splynx_archived_quote_items_pkey PRIMARY KEY (id);


--
-- Name: splynx_archived_quotes splynx_archived_quotes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_quotes
    ADD CONSTRAINT splynx_archived_quotes_pkey PRIMARY KEY (id);


--
-- Name: splynx_archived_quotes splynx_archived_quotes_splynx_quote_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_quotes
    ADD CONSTRAINT splynx_archived_quotes_splynx_quote_id_key UNIQUE (splynx_quote_id);


--
-- Name: splynx_archived_ticket_messages splynx_archived_ticket_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_ticket_messages
    ADD CONSTRAINT splynx_archived_ticket_messages_pkey PRIMARY KEY (id);


--
-- Name: splynx_archived_ticket_messages splynx_archived_ticket_messages_splynx_message_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_ticket_messages
    ADD CONSTRAINT splynx_archived_ticket_messages_splynx_message_id_key UNIQUE (splynx_message_id);


--
-- Name: splynx_archived_tickets splynx_archived_tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_tickets
    ADD CONSTRAINT splynx_archived_tickets_pkey PRIMARY KEY (id);


--
-- Name: splynx_archived_tickets splynx_archived_tickets_splynx_ticket_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_tickets
    ADD CONSTRAINT splynx_archived_tickets_splynx_ticket_id_key UNIQUE (splynx_ticket_id);


--
-- Name: splynx_id_mappings splynx_id_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_id_mappings
    ADD CONSTRAINT splynx_id_mappings_pkey PRIMARY KEY (id);


--
-- Name: stored_files stored_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stored_files
    ADD CONSTRAINT stored_files_pkey PRIMARY KEY (id);


--
-- Name: subscriber_channels subscriber_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_channels
    ADD CONSTRAINT subscriber_channels_pkey PRIMARY KEY (id);


--
-- Name: subscriber_custom_fields subscriber_custom_fields_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_custom_fields
    ADD CONSTRAINT subscriber_custom_fields_pkey PRIMARY KEY (id);


--
-- Name: subscriber_permissions subscriber_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_permissions
    ADD CONSTRAINT subscriber_permissions_pkey PRIMARY KEY (id);


--
-- Name: subscriber_roles subscriber_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_roles
    ADD CONSTRAINT subscriber_roles_pkey PRIMARY KEY (id);


--
-- Name: subscribers subscribers_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscribers
    ADD CONSTRAINT subscribers_email_key UNIQUE (email);


--
-- Name: subscribers subscribers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscribers
    ADD CONSTRAINT subscribers_pkey PRIMARY KEY (id);


--
-- Name: subscribers subscribers_subscriber_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscribers
    ADD CONSTRAINT subscribers_subscriber_number_key UNIQUE (subscriber_number);


--
-- Name: subscription_add_ons subscription_add_ons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_add_ons
    ADD CONSTRAINT subscription_add_ons_pkey PRIMARY KEY (id);


--
-- Name: subscription_change_requests subscription_change_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_change_requests
    ADD CONSTRAINT subscription_change_requests_pkey PRIMARY KEY (id);


--
-- Name: subscription_engine_settings subscription_engine_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_engine_settings
    ADD CONSTRAINT subscription_engine_settings_pkey PRIMARY KEY (id);


--
-- Name: subscription_engines subscription_engines_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_engines
    ADD CONSTRAINT subscription_engines_code_key UNIQUE (code);


--
-- Name: subscription_engines subscription_engines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_engines
    ADD CONSTRAINT subscription_engines_pkey PRIMARY KEY (id);


--
-- Name: subscription_lifecycle_events subscription_lifecycle_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_lifecycle_events
    ADD CONSTRAINT subscription_lifecycle_events_pkey PRIMARY KEY (id);


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


--
-- Name: support_ticket_comments support_ticket_comments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_comments
    ADD CONSTRAINT support_ticket_comments_pkey PRIMARY KEY (id);


--
-- Name: support_ticket_links support_ticket_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_links
    ADD CONSTRAINT support_ticket_links_pkey PRIMARY KEY (id);


--
-- Name: support_ticket_sla_events support_ticket_sla_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_sla_events
    ADD CONSTRAINT support_ticket_sla_events_pkey PRIMARY KEY (id);


--
-- Name: support_tickets support_tickets_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_number_key UNIQUE (number);


--
-- Name: support_tickets support_tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_pkey PRIMARY KEY (id);


--
-- Name: survey_responses survey_responses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.survey_responses
    ADD CONSTRAINT survey_responses_pkey PRIMARY KEY (id);


--
-- Name: surveys surveys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.surveys
    ADD CONSTRAINT surveys_pkey PRIMARY KEY (id);


--
-- Name: system_user_permissions system_user_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_permissions
    ADD CONSTRAINT system_user_permissions_pkey PRIMARY KEY (id);


--
-- Name: system_user_roles system_user_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_roles
    ADD CONSTRAINT system_user_roles_pkey PRIMARY KEY (id);


--
-- Name: system_users system_users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_users
    ADD CONSTRAINT system_users_email_key UNIQUE (email);


--
-- Name: system_users system_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_users
    ADD CONSTRAINT system_users_pkey PRIMARY KEY (id);


--
-- Name: table_column_config table_column_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_column_config
    ADD CONSTRAINT table_column_config_pkey PRIMARY KEY (id);


--
-- Name: table_column_default_config table_column_default_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_column_default_config
    ADD CONSTRAINT table_column_default_config_pkey PRIMARY KEY (id);


--
-- Name: tax_rates tax_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_rates
    ADD CONSTRAINT tax_rates_pkey PRIMARY KEY (id);


--
-- Name: tr069_acs_servers tr069_acs_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_acs_servers
    ADD CONSTRAINT tr069_acs_servers_pkey PRIMARY KEY (id);


--
-- Name: tr069_cpe_devices tr069_cpe_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_cpe_devices
    ADD CONSTRAINT tr069_cpe_devices_pkey PRIMARY KEY (id);


--
-- Name: tr069_jobs tr069_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_jobs
    ADD CONSTRAINT tr069_jobs_pkey PRIMARY KEY (id);


--
-- Name: tr069_parameter_maps tr069_parameter_maps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_parameter_maps
    ADD CONSTRAINT tr069_parameter_maps_pkey PRIMARY KEY (id);


--
-- Name: tr069_parameters tr069_parameters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_parameters
    ADD CONSTRAINT tr069_parameters_pkey PRIMARY KEY (id);


--
-- Name: tr069_sessions tr069_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_sessions
    ADD CONSTRAINT tr069_sessions_pkey PRIMARY KEY (id);


--
-- Name: access_credentials uq_access_credentials_username; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_credentials
    ADD CONSTRAINT uq_access_credentials_username UNIQUE (username);


--
-- Name: alert_notification_policies uq_alert_notification_policies_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policies
    ADD CONSTRAINT uq_alert_notification_policies_name UNIQUE (name);


--
-- Name: collection_accounts uq_collection_accounts_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_accounts
    ADD CONSTRAINT uq_collection_accounts_name UNIQUE (name);


--
-- Name: connector_configs uq_connector_configs_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_configs
    ADD CONSTRAINT uq_connector_configs_name UNIQUE (name);


--
-- Name: domain_settings uq_domain_settings_domain_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_settings
    ADD CONSTRAINT uq_domain_settings_domain_key UNIQUE (domain, key);


--
-- Name: external_references uq_external_refs_connector_entity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_references
    ADD CONSTRAINT uq_external_refs_connector_entity UNIQUE (connector_config_id, entity_type, entity_id);


--
-- Name: external_references uq_external_refs_connector_external; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_references
    ADD CONSTRAINT uq_external_refs_connector_external UNIQUE (connector_config_id, entity_type, external_id);


--
-- Name: fdh_cabinets uq_fdh_cabinets_code; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fdh_cabinets
    ADD CONSTRAINT uq_fdh_cabinets_code UNIQUE (code);


--
-- Name: fiber_segments uq_fiber_segments_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_segments
    ADD CONSTRAINT uq_fiber_segments_name UNIQUE (name);


--
-- Name: fiber_splice_trays uq_fiber_splice_trays_closure_tray; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splice_trays
    ADD CONSTRAINT uq_fiber_splice_trays_closure_tray UNIQUE (closure_id, tray_number);


--
-- Name: fiber_splices uq_fiber_splices_from_to; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splices
    ADD CONSTRAINT uq_fiber_splices_from_to UNIQUE (from_strand_id, to_strand_id);


--
-- Name: fiber_splices uq_fiber_splices_tray_position; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splices
    ADD CONSTRAINT uq_fiber_splices_tray_position UNIQUE (tray_id, "position");


--
-- Name: fiber_strands uq_fiber_strands_cable_strand; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_strands
    ADD CONSTRAINT uq_fiber_strands_cable_strand UNIQUE (cable_name, strand_number);


--
-- Name: fup_states uq_fup_states_subscription; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_states
    ADD CONSTRAINT uq_fup_states_subscription UNIQUE (subscription_id);


--
-- Name: geo_layers uq_geo_layers_layer_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geo_layers
    ADD CONSTRAINT uq_geo_layers_layer_key UNIQUE (layer_key);


--
-- Name: ip_assignments uq_ip_assignments_ipv4_address_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT uq_ip_assignments_ipv4_address_id UNIQUE (ipv4_address_id);


--
-- Name: ip_assignments uq_ip_assignments_ipv6_address_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT uq_ip_assignments_ipv6_address_id UNIQUE (ipv6_address_id);


--
-- Name: ip_blocks uq_ip_blocks_pool_cidr; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_blocks
    ADD CONSTRAINT uq_ip_blocks_pool_cidr UNIQUE (pool_id, cidr);


--
-- Name: ip_pools uq_ip_pools_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_pools
    ADD CONSTRAINT uq_ip_pools_name UNIQUE (name);


--
-- Name: ipv4_addresses uq_ipv4_addresses_address; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ipv4_addresses
    ADD CONSTRAINT uq_ipv4_addresses_address UNIQUE (address);


--
-- Name: ipv6_addresses uq_ipv6_addresses_address; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ipv6_addresses
    ADD CONSTRAINT uq_ipv6_addresses_address UNIQUE (address);


--
-- Name: mrr_snapshots uq_mrr_snapshot_subscriber_date; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mrr_snapshots
    ADD CONSTRAINT uq_mrr_snapshot_subscriber_date UNIQUE (subscriber_id, snapshot_date);


--
-- Name: nas_connection_rules uq_nas_connection_rules_device_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_connection_rules
    ADD CONSTRAINT uq_nas_connection_rules_device_name UNIQUE (nas_device_id, name);


--
-- Name: network_devices uq_network_devices_hostname; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_devices
    ADD CONSTRAINT uq_network_devices_hostname UNIQUE (hostname);


--
-- Name: network_devices uq_network_devices_mgmt_ip; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_devices
    ADD CONSTRAINT uq_network_devices_mgmt_ip UNIQUE (mgmt_ip);


--
-- Name: network_zones uq_network_zones_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_zones
    ADD CONSTRAINT uq_network_zones_name UNIQUE (name);


--
-- Name: notification_templates uq_notification_templates_code_channel; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_templates
    ADD CONSTRAINT uq_notification_templates_code_channel UNIQUE (code, channel);


--
-- Name: oauth_tokens uq_oauth_tokens_connector_provider_account; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT uq_oauth_tokens_connector_provider_account UNIQUE (connector_config_id, provider, external_account_id);


--
-- Name: offer_billing_mode_availability uq_offer_billing_mode; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_billing_mode_availability
    ADD CONSTRAINT uq_offer_billing_mode UNIQUE (offer_id, billing_mode);


--
-- Name: offer_category_availability uq_offer_category; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_category_availability
    ADD CONSTRAINT uq_offer_category UNIQUE (offer_id, subscriber_category);


--
-- Name: offer_location_availability uq_offer_location; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_location_availability
    ADD CONSTRAINT uq_offer_location UNIQUE (offer_id, pop_site_id);


--
-- Name: offer_reseller_availability uq_offer_reseller; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_reseller_availability
    ADD CONSTRAINT uq_offer_reseller UNIQUE (offer_id, reseller_id);


--
-- Name: olt_autofind_candidates uq_olt_autofind_candidates_olt_fsp_serial; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_autofind_candidates
    ADD CONSTRAINT uq_olt_autofind_candidates_olt_fsp_serial UNIQUE (olt_id, fsp, serial_number);


--
-- Name: olt_card_ports uq_olt_card_ports_card_port_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_card_ports
    ADD CONSTRAINT uq_olt_card_ports_card_port_number UNIQUE (card_id, port_number);


--
-- Name: olt_cards uq_olt_cards_shelf_slot_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_cards
    ADD CONSTRAINT uq_olt_cards_shelf_slot_number UNIQUE (shelf_id, slot_number);


--
-- Name: olt_devices uq_olt_devices_hostname; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_devices
    ADD CONSTRAINT uq_olt_devices_hostname UNIQUE (hostname);


--
-- Name: olt_devices uq_olt_devices_mgmt_ip; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_devices
    ADD CONSTRAINT uq_olt_devices_mgmt_ip UNIQUE (mgmt_ip);


--
-- Name: olt_firmware_images uq_olt_firmware_vendor_model_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_firmware_images
    ADD CONSTRAINT uq_olt_firmware_vendor_model_version UNIQUE (vendor, model, version);


--
-- Name: olt_power_units uq_olt_power_units_olt_slot; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_power_units
    ADD CONSTRAINT uq_olt_power_units_olt_slot UNIQUE (olt_id, slot);


--
-- Name: olt_sfp_modules uq_olt_sfp_modules_port_serial; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_sfp_modules
    ADD CONSTRAINT uq_olt_sfp_modules_port_serial UNIQUE (olt_card_port_id, serial_number);


--
-- Name: olt_shelves uq_olt_shelves_olt_shelf_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_shelves
    ADD CONSTRAINT uq_olt_shelves_olt_shelf_number UNIQUE (olt_id, shelf_number);


--
-- Name: on_call_rotations uq_on_call_rotations_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.on_call_rotations
    ADD CONSTRAINT uq_on_call_rotations_name UNIQUE (name);


--
-- Name: ont_firmware_images uq_ont_firmware_vendor_model_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_firmware_images
    ADD CONSTRAINT uq_ont_firmware_vendor_model_version UNIQUE (vendor, model, version);


--
-- Name: ont_provisioning_profiles uq_ont_prov_profiles_owner_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_provisioning_profiles
    ADD CONSTRAINT uq_ont_prov_profiles_owner_name UNIQUE (owner_subscriber_id, name);


--
-- Name: ont_units uq_ont_units_serial_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT uq_ont_units_serial_number UNIQUE (serial_number);


--
-- Name: onu_types uq_onu_types_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onu_types
    ADD CONSTRAINT uq_onu_types_name UNIQUE (name);


--
-- Name: payment_allocations uq_payment_allocations_payment_invoice; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocations
    ADD CONSTRAINT uq_payment_allocations_payment_invoice UNIQUE (payment_id, invoice_id);


--
-- Name: payment_channel_accounts uq_payment_channel_accounts_channel_account_currency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_channel_accounts
    ADD CONSTRAINT uq_payment_channel_accounts_channel_account_currency UNIQUE (channel_id, collection_account_id, currency);


--
-- Name: payment_channels uq_payment_channels_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_channels
    ADD CONSTRAINT uq_payment_channels_name UNIQUE (name);


--
-- Name: payment_provider_events uq_payment_provider_events_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_provider_events
    ADD CONSTRAINT uq_payment_provider_events_idempotency UNIQUE (provider_id, idempotency_key);


--
-- Name: payment_providers uq_payment_providers_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_providers
    ADD CONSTRAINT uq_payment_providers_name UNIQUE (name);


--
-- Name: permissions uq_permissions_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT uq_permissions_key UNIQUE (key);


--
-- Name: pon_port_splitter_links uq_pon_port_splitter_links_pon_port; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pon_port_splitter_links
    ADD CONSTRAINT uq_pon_port_splitter_links_pon_port UNIQUE (pon_port_id);


--
-- Name: pon_ports uq_pon_ports_olt_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pon_ports
    ADD CONSTRAINT uq_pon_ports_olt_name UNIQUE (olt_id, name);


--
-- Name: port_vlans uq_port_vlans_port_vlan; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.port_vlans
    ADD CONSTRAINT uq_port_vlans_port_vlan UNIQUE (port_id, vlan_id);


--
-- Name: queue_mappings uq_queue_mappings_device_queue; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.queue_mappings
    ADD CONSTRAINT uq_queue_mappings_device_queue UNIQUE (nas_device_id, queue_name);


--
-- Name: radius_active_sessions uq_radius_active_session; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_active_sessions
    ADD CONSTRAINT uq_radius_active_session UNIQUE (acct_session_id, nas_device_id);


--
-- Name: radius_clients uq_radius_clients_server_ip; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_clients
    ADD CONSTRAINT uq_radius_clients_server_ip UNIQUE (server_id, client_ip);


--
-- Name: radius_servers uq_radius_servers_host_ports; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_servers
    ADD CONSTRAINT uq_radius_servers_host_ports UNIQUE (host, auth_port, acct_port);


--
-- Name: radius_users uq_radius_users_access_credential; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_users
    ADD CONSTRAINT uq_radius_users_access_credential UNIQUE (access_credential_id);


--
-- Name: radius_users uq_radius_users_username; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_users
    ADD CONSTRAINT uq_radius_users_username UNIQUE (username);


--
-- Name: role_permissions uq_role_permissions_role_permission; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT uq_role_permissions_role_permission UNIQUE (role_id, permission_id);


--
-- Name: roles uq_roles_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT uq_roles_name UNIQUE (name);


--
-- Name: router_interfaces uq_router_interface_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_interfaces
    ADD CONSTRAINT uq_router_interface_name UNIQUE (router_id, name);


--
-- Name: speed_profiles uq_speed_profiles_name_direction; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.speed_profiles
    ADD CONSTRAINT uq_speed_profiles_name_direction UNIQUE (name, direction);


--
-- Name: splitter_port_assignments uq_splitter_port_assignments_port_active; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_port_assignments
    ADD CONSTRAINT uq_splitter_port_assignments_port_active UNIQUE (splitter_port_id, active);


--
-- Name: splitter_ports uq_splitter_ports_splitter_port_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_ports
    ADD CONSTRAINT uq_splitter_ports_splitter_port_number UNIQUE (splitter_id, port_number);


--
-- Name: splitters uq_splitters_fdh_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitters
    ADD CONSTRAINT uq_splitters_fdh_name UNIQUE (fdh_id, name);


--
-- Name: splynx_id_mappings uq_splynx_mapping_type_dotmac_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_id_mappings
    ADD CONSTRAINT uq_splynx_mapping_type_dotmac_id UNIQUE (entity_type, dotmac_id);


--
-- Name: splynx_id_mappings uq_splynx_mapping_type_splynx_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_id_mappings
    ADD CONSTRAINT uq_splynx_mapping_type_splynx_id UNIQUE (entity_type, splynx_id);


--
-- Name: subscriber_channels uq_subscriber_channels_subscriber_type_address; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_channels
    ADD CONSTRAINT uq_subscriber_channels_subscriber_type_address UNIQUE (subscriber_id, channel_type, address);


--
-- Name: subscriber_custom_fields uq_subscriber_custom_fields_subscriber_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_custom_fields
    ADD CONSTRAINT uq_subscriber_custom_fields_subscriber_key UNIQUE (subscriber_id, key);


--
-- Name: subscriber_permissions uq_subscriber_permissions_subscriber_permission; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_permissions
    ADD CONSTRAINT uq_subscriber_permissions_subscriber_permission UNIQUE (subscriber_id, permission_id);


--
-- Name: subscriber_roles uq_subscriber_roles_subscriber_role; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_roles
    ADD CONSTRAINT uq_subscriber_roles_subscriber_role UNIQUE (subscriber_id, role_id);


--
-- Name: support_ticket_assignees uq_support_ticket_assignee; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_assignees
    ADD CONSTRAINT uq_support_ticket_assignee PRIMARY KEY (ticket_id, person_id);


--
-- Name: support_ticket_links uq_support_ticket_link; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_links
    ADD CONSTRAINT uq_support_ticket_link UNIQUE (from_ticket_id, to_ticket_id, link_type);


--
-- Name: support_ticket_merges uq_support_ticket_merge_pair; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_merges
    ADD CONSTRAINT uq_support_ticket_merge_pair PRIMARY KEY (source_ticket_id, target_ticket_id);


--
-- Name: system_user_permissions uq_system_user_permissions_user_permission; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_permissions
    ADD CONSTRAINT uq_system_user_permissions_user_permission UNIQUE (system_user_id, permission_id);


--
-- Name: system_user_roles uq_system_user_roles_user_role; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_roles
    ADD CONSTRAINT uq_system_user_roles_user_role UNIQUE (system_user_id, role_id);


--
-- Name: table_column_config uq_table_column_config_user_table_column; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_column_config
    ADD CONSTRAINT uq_table_column_config_user_table_column UNIQUE (user_id, table_key, column_key);


--
-- Name: table_column_default_config uq_table_column_default_config_table_column; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_column_default_config
    ADD CONSTRAINT uq_table_column_default_config_table_column UNIQUE (table_key, column_key);


--
-- Name: network_topology_links uq_topology_link_endpoints; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_topology_links
    ADD CONSTRAINT uq_topology_link_endpoints UNIQUE (source_device_id, source_interface_id, target_device_id, target_interface_id);


--
-- Name: tr069_parameter_maps uq_tr069_param_cap_canonical; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_parameter_maps
    ADD CONSTRAINT uq_tr069_param_cap_canonical UNIQUE (capability_id, canonical_name);


--
-- Name: vlans uq_vlans_region_tag; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vlans
    ADD CONSTRAINT uq_vlans_region_tag UNIQUE (region_id, tag);


--
-- Name: vendor_model_capabilities uq_vmc_vendor_model_fw; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_model_capabilities
    ADD CONSTRAINT uq_vmc_vendor_model_fw UNIQUE (vendor, model, firmware_pattern);


--
-- Name: webhook_endpoints uq_webhook_endpoints_url; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_endpoints
    ADD CONSTRAINT uq_webhook_endpoints_url UNIQUE (url);


--
-- Name: webhook_subscriptions uq_webhook_subscriptions_endpoint_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_subscriptions
    ADD CONSTRAINT uq_webhook_subscriptions_endpoint_event UNIQUE (endpoint_id, event_type);


--
-- Name: usage_allowances usage_allowances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_allowances
    ADD CONSTRAINT usage_allowances_pkey PRIMARY KEY (id);


--
-- Name: usage_charges usage_charges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_charges
    ADD CONSTRAINT usage_charges_pkey PRIMARY KEY (id);


--
-- Name: usage_rating_runs usage_rating_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rating_runs
    ADD CONSTRAINT usage_rating_runs_pkey PRIMARY KEY (id);


--
-- Name: usage_records usage_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_records
    ADD CONSTRAINT usage_records_pkey PRIMARY KEY (id);


--
-- Name: user_credentials user_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_credentials
    ADD CONSTRAINT user_credentials_pkey PRIMARY KEY (id);


--
-- Name: vendor_model_capabilities vendor_model_capabilities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_model_capabilities
    ADD CONSTRAINT vendor_model_capabilities_pkey PRIMARY KEY (id);


--
-- Name: vlans vlans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vlans
    ADD CONSTRAINT vlans_pkey PRIMARY KEY (id);


--
-- Name: webhook_deliveries webhook_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_pkey PRIMARY KEY (id);


--
-- Name: webhook_endpoints webhook_endpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_endpoints
    ADD CONSTRAINT webhook_endpoints_pkey PRIMARY KEY (id);


--
-- Name: webhook_subscriptions webhook_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_subscriptions
    ADD CONSTRAINT webhook_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: wireguard_connection_logs wireguard_connection_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wireguard_connection_logs
    ADD CONSTRAINT wireguard_connection_logs_pkey PRIMARY KEY (id);


--
-- Name: wireguard_peers wireguard_peers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wireguard_peers
    ADD CONSTRAINT wireguard_peers_pkey PRIMARY KEY (id);


--
-- Name: wireguard_servers wireguard_servers_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wireguard_servers
    ADD CONSTRAINT wireguard_servers_name_key UNIQUE (name);


--
-- Name: wireguard_servers wireguard_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wireguard_servers
    ADD CONSTRAINT wireguard_servers_pkey PRIMARY KEY (id);


--
-- Name: idx_addresses_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_addresses_geom ON public.addresses USING gist (geom);


--
-- Name: idx_coverage_areas_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coverage_areas_geom ON public.coverage_areas USING gist (geom);


--
-- Name: idx_fdh_cabinets_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fdh_cabinets_geom ON public.fdh_cabinets USING gist (geom);


--
-- Name: idx_fiber_access_points_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fiber_access_points_geom ON public.fiber_access_points USING gist (geom);


--
-- Name: idx_fiber_segments_route_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fiber_segments_route_geom ON public.fiber_segments USING gist (route_geom);


--
-- Name: idx_fiber_splice_closures_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fiber_splice_closures_geom ON public.fiber_splice_closures USING gist (geom);


--
-- Name: idx_geo_areas_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_geo_areas_geom ON public.geo_areas USING gist (geom);


--
-- Name: idx_geo_locations_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_geo_locations_geom ON public.geo_locations USING gist (geom);


--
-- Name: idx_network_zones_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_network_zones_geom ON public.network_zones USING gist (geom);


--
-- Name: idx_pop_sites_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pop_sites_geom ON public.pop_sites USING gist (geom);


--
-- Name: idx_service_buildings_boundary_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_service_buildings_boundary_geom ON public.service_buildings USING gist (boundary_geom);


--
-- Name: idx_service_buildings_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_service_buildings_geom ON public.service_buildings USING gist (geom);


--
-- Name: idx_service_qualifications_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_service_qualifications_geom ON public.service_qualifications USING gist (geom);


--
-- Name: ix_communication_logs_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_communication_logs_channel ON public.communication_logs USING btree (channel);


--
-- Name: ix_communication_logs_sent_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_communication_logs_sent_at ON public.communication_logs USING btree (sent_at);


--
-- Name: ix_communication_logs_subscriber; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_communication_logs_subscriber ON public.communication_logs USING btree (subscriber_id);


--
-- Name: ix_enforcement_locks_subscriber_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_enforcement_locks_subscriber_active ON public.enforcement_locks USING btree (subscriber_id, is_active);


--
-- Name: ix_enforcement_locks_subscription_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_enforcement_locks_subscription_active ON public.enforcement_locks USING btree (subscription_id, is_active);


--
-- Name: ix_event_store_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_store_account_id ON public.event_store USING btree (account_id);


--
-- Name: ix_event_store_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_event_store_event_id ON public.event_store USING btree (event_id);


--
-- Name: ix_event_store_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_store_event_type ON public.event_store USING btree (event_type);


--
-- Name: ix_event_store_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_store_status ON public.event_store USING btree (status);


--
-- Name: ix_event_store_subscriber_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_store_subscriber_id ON public.event_store USING btree (subscriber_id);


--
-- Name: ix_fiber_change_requests_asset_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fiber_change_requests_asset_type ON public.fiber_change_requests USING btree (asset_type);


--
-- Name: ix_fiber_change_requests_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fiber_change_requests_status ON public.fiber_change_requests USING btree (status);


--
-- Name: ix_ip_pools_nas_device_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ip_pools_nas_device_id ON public.ip_pools USING btree (nas_device_id);


--
-- Name: ix_ip_pools_olt_device_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ip_pools_olt_device_id ON public.ip_pools USING btree (olt_device_id);


--
-- Name: ix_mfa_methods_primary_per_subscriber; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_mfa_methods_primary_per_subscriber ON public.mfa_methods USING btree (subscriber_id) WHERE is_primary;


--
-- Name: ix_mfa_methods_primary_per_system_user; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_mfa_methods_primary_per_system_user ON public.mfa_methods USING btree (system_user_id) WHERE is_primary;


--
-- Name: ix_mrr_snapshots_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mrr_snapshots_date ON public.mrr_snapshots USING btree (snapshot_date);


--
-- Name: ix_mrr_snapshots_subscriber; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mrr_snapshots_subscriber ON public.mrr_snapshots USING btree (subscriber_id);


--
-- Name: ix_netops_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_netops_parent ON public.network_operations USING btree (parent_id);


--
-- Name: ix_netops_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_netops_status ON public.network_operations USING btree (status);


--
-- Name: ix_netops_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_netops_target ON public.network_operations USING btree (target_type, target_id);


--
-- Name: ix_oauth_tokens_connector_config_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_connector_config_id ON public.oauth_tokens USING btree (connector_config_id);


--
-- Name: ix_oauth_tokens_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_provider ON public.oauth_tokens USING btree (provider);


--
-- Name: ix_oauth_tokens_token_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_token_expires_at ON public.oauth_tokens USING btree (token_expires_at);


--
-- Name: ix_olt_autofind_candidates_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_olt_autofind_candidates_active ON public.olt_autofind_candidates USING btree (is_active);


--
-- Name: ix_olt_autofind_candidates_olt_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_olt_autofind_candidates_olt_active ON public.olt_autofind_candidates USING btree (olt_id, is_active);


--
-- Name: ix_olt_config_backups_olt_device_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_olt_config_backups_olt_device_id ON public.olt_config_backups USING btree (olt_device_id);


--
-- Name: ix_ont_assignments_active_unit; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_ont_assignments_active_unit ON public.ont_assignments USING btree (ont_unit_id) WHERE active;


--
-- Name: ix_pop_sites_owner_subscriber_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pop_sites_owner_subscriber_id ON public.pop_sites USING btree (owner_subscriber_id);


--
-- Name: ix_portal_messages_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_portal_messages_status ON public.portal_messages USING btree (status);


--
-- Name: ix_portal_messages_subscriber; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_portal_messages_subscriber ON public.portal_messages USING btree (subscriber_id);


--
-- Name: ix_push_results_push_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_push_results_push_id ON public.router_config_push_results USING btree (push_id);


--
-- Name: ix_radius_auth_errors_nas; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_radius_auth_errors_nas ON public.radius_auth_errors USING btree (nas_device_id);


--
-- Name: ix_radius_auth_errors_occurred_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_radius_auth_errors_occurred_at ON public.radius_auth_errors USING btree (occurred_at);


--
-- Name: ix_radius_auth_errors_subscriber; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_radius_auth_errors_subscriber ON public.radius_auth_errors USING btree (subscriber_id);


--
-- Name: ix_radius_auth_errors_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_radius_auth_errors_username ON public.radius_auth_errors USING btree (username);


--
-- Name: ix_router_config_snapshots_router_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_router_config_snapshots_router_id ON public.router_config_snapshots USING btree (router_id);


--
-- Name: ix_routers_management_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_routers_management_ip ON public.routers USING btree (management_ip);


--
-- Name: ix_routers_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_routers_status ON public.routers USING btree (status);


--
-- Name: ix_stored_files_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_stored_files_entity ON public.stored_files USING btree (entity_type, entity_id);


--
-- Name: ix_stored_files_owner_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_stored_files_owner_active ON public.stored_files USING btree (owner_subscriber_id, is_deleted);


--
-- Name: ix_subscribers_pop_site_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_subscribers_pop_site_id ON public.subscribers USING btree (pop_site_id);


--
-- Name: uq_communication_logs_channel_external_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_communication_logs_channel_external_id ON public.communication_logs USING btree (channel, external_id) WHERE (external_id IS NOT NULL);


--
-- Name: uq_communication_logs_channel_splynx_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_communication_logs_channel_splynx_message_id ON public.communication_logs USING btree (channel, splynx_message_id) WHERE (splynx_message_id IS NOT NULL);


--
-- Name: uq_invoices_active_splynx_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_invoices_active_splynx_invoice_id ON public.invoices USING btree (splynx_invoice_id) WHERE ((is_active = true) AND (splynx_invoice_id IS NOT NULL));


--
-- Name: uq_network_devices_active_splynx_monitoring_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_network_devices_active_splynx_monitoring_id ON public.network_devices USING btree (splynx_monitoring_id) WHERE ((is_active = true) AND (splynx_monitoring_id IS NOT NULL));


--
-- Name: uq_notification_deliveries_provider_message; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_notification_deliveries_provider_message ON public.notification_deliveries USING btree (provider, provider_message_id) WHERE ((is_active = true) AND (provider IS NOT NULL) AND (provider_message_id IS NOT NULL));


--
-- Name: uq_ont_units_olt_external_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ont_units_olt_external_id ON public.ont_units USING btree (olt_device_id, external_id) WHERE ((olt_device_id IS NOT NULL) AND (external_id IS NOT NULL));


--
-- Name: uq_payment_provider_events_external_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payment_provider_events_external_id ON public.payment_provider_events USING btree (provider_id, external_id) WHERE (external_id IS NOT NULL);


--
-- Name: uq_payments_active_external_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payments_active_external_id ON public.payments USING btree (provider_id, external_id) WHERE ((is_active = true) AND (provider_id IS NOT NULL) AND (external_id IS NOT NULL));


--
-- Name: uq_payments_active_splynx_payment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payments_active_splynx_payment_id ON public.payments USING btree (splynx_payment_id) WHERE ((is_active = true) AND (splynx_payment_id IS NOT NULL));


--
-- Name: uq_subscribers_splynx_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_subscribers_splynx_customer_id ON public.subscribers USING btree (splynx_customer_id) WHERE (splynx_customer_id IS NOT NULL);


--
-- Name: uq_tr069_cpe_devices_active_genieacs_device_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_tr069_cpe_devices_active_genieacs_device_id ON public.tr069_cpe_devices USING btree (genieacs_device_id) WHERE ((is_active = true) AND (genieacs_device_id IS NOT NULL));


--
-- Name: ix_support_ticket_comments_ticket; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_support_ticket_comments_ticket ON public.support_ticket_comments USING btree (ticket_id);


--
-- Name: ix_support_ticket_sla_events_ticket; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_support_ticket_sla_events_ticket ON public.support_ticket_sla_events USING btree (ticket_id);


--
-- Name: ix_support_tickets_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_support_tickets_active ON public.support_tickets USING btree (is_active);


--
-- Name: ix_support_tickets_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_support_tickets_number ON public.support_tickets USING btree (number);


--
-- Name: ix_support_tickets_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_support_tickets_priority ON public.support_tickets USING btree (priority);


--
-- Name: ix_support_tickets_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_support_tickets_status ON public.support_tickets USING btree (status);


--
-- Name: ix_support_tickets_subscriber; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_support_tickets_subscriber ON public.support_tickets USING btree (subscriber_id);


--
-- Name: ix_topology_link_bundle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_topology_link_bundle ON public.network_topology_links USING btree (bundle_key);


--
-- Name: ix_topology_link_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_topology_link_group ON public.network_topology_links USING btree (topology_group);


--
-- Name: ix_topology_link_source_device; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_topology_link_source_device ON public.network_topology_links USING btree (source_device_id);


--
-- Name: ix_topology_link_source_iface; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_topology_link_source_iface ON public.network_topology_links USING btree (source_interface_id);


--
-- Name: ix_topology_link_target_device; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_topology_link_target_device ON public.network_topology_links USING btree (target_device_id);


--
-- Name: ix_topology_link_target_iface; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_topology_link_target_iface ON public.network_topology_links USING btree (target_interface_id);


--
-- Name: ix_user_credentials_local_username_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_user_credentials_local_username_unique ON public.user_credentials USING btree (username) WHERE (provider = 'local'::public.authprovider);


--
-- Name: ix_vlans_olt_device_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vlans_olt_device_id ON public.vlans USING btree (olt_device_id);


--
-- Name: uq_enforcement_locks_active_reason; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_enforcement_locks_active_reason ON public.enforcement_locks USING btree (subscription_id, reason) WHERE (is_active = true);


--
-- Name: ux_sessions_previous_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_sessions_previous_token_hash ON public.sessions USING btree (previous_token_hash);


--
-- Name: ux_sessions_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_sessions_token_hash ON public.sessions USING btree (token_hash);


--
-- Name: access_credentials access_credentials_radius_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_credentials
    ADD CONSTRAINT access_credentials_radius_profile_id_fkey FOREIGN KEY (radius_profile_id) REFERENCES public.radius_profiles(id);


--
-- Name: access_credentials access_credentials_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_credentials
    ADD CONSTRAINT access_credentials_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: add_on_prices add_on_prices_add_on_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.add_on_prices
    ADD CONSTRAINT add_on_prices_add_on_id_fkey FOREIGN KEY (add_on_id) REFERENCES public.add_ons(id);


--
-- Name: addresses addresses_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.addresses
    ADD CONSTRAINT addresses_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: addresses addresses_tax_rate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.addresses
    ADD CONSTRAINT addresses_tax_rate_id_fkey FOREIGN KEY (tax_rate_id) REFERENCES public.tax_rates(id);


--
-- Name: alert_events alert_events_alert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_events
    ADD CONSTRAINT alert_events_alert_id_fkey FOREIGN KEY (alert_id) REFERENCES public.alerts(id);


--
-- Name: alert_notification_logs alert_notification_logs_alert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_logs
    ADD CONSTRAINT alert_notification_logs_alert_id_fkey FOREIGN KEY (alert_id) REFERENCES public.alerts(id);


--
-- Name: alert_notification_logs alert_notification_logs_notification_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_logs
    ADD CONSTRAINT alert_notification_logs_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.notifications(id);


--
-- Name: alert_notification_logs alert_notification_logs_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_logs
    ADD CONSTRAINT alert_notification_logs_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES public.alert_notification_policies(id);


--
-- Name: alert_notification_policies alert_notification_policies_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policies
    ADD CONSTRAINT alert_notification_policies_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: alert_notification_policies alert_notification_policies_interface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policies
    ADD CONSTRAINT alert_notification_policies_interface_id_fkey FOREIGN KEY (interface_id) REFERENCES public.device_interfaces(id);


--
-- Name: alert_notification_policies alert_notification_policies_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policies
    ADD CONSTRAINT alert_notification_policies_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.alert_rules(id);


--
-- Name: alert_notification_policies alert_notification_policies_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policies
    ADD CONSTRAINT alert_notification_policies_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.notification_templates(id);


--
-- Name: alert_notification_policy_steps alert_notification_policy_steps_connector_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policy_steps
    ADD CONSTRAINT alert_notification_policy_steps_connector_config_id_fkey FOREIGN KEY (connector_config_id) REFERENCES public.connector_configs(id);


--
-- Name: alert_notification_policy_steps alert_notification_policy_steps_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policy_steps
    ADD CONSTRAINT alert_notification_policy_steps_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES public.alert_notification_policies(id);


--
-- Name: alert_notification_policy_steps alert_notification_policy_steps_rotation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policy_steps
    ADD CONSTRAINT alert_notification_policy_steps_rotation_id_fkey FOREIGN KEY (rotation_id) REFERENCES public.on_call_rotations(id);


--
-- Name: alert_notification_policy_steps alert_notification_policy_steps_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_notification_policy_steps
    ADD CONSTRAINT alert_notification_policy_steps_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.notification_templates(id);


--
-- Name: alert_rules alert_rules_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_rules
    ADD CONSTRAINT alert_rules_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: alert_rules alert_rules_interface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_rules
    ADD CONSTRAINT alert_rules_interface_id_fkey FOREIGN KEY (interface_id) REFERENCES public.device_interfaces(id);


--
-- Name: alerts alerts_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: alerts alerts_interface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_interface_id_fkey FOREIGN KEY (interface_id) REFERENCES public.device_interfaces(id);


--
-- Name: alerts alerts_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.alert_rules(id);


--
-- Name: api_keys api_keys_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: api_keys api_keys_system_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_system_user_id_fkey FOREIGN KEY (system_user_id) REFERENCES public.system_users(id);


--
-- Name: bandwidth_samples bandwidth_samples_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bandwidth_samples
    ADD CONSTRAINT bandwidth_samples_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: bandwidth_samples bandwidth_samples_interface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bandwidth_samples
    ADD CONSTRAINT bandwidth_samples_interface_id_fkey FOREIGN KEY (interface_id) REFERENCES public.device_interfaces(id);


--
-- Name: bandwidth_samples bandwidth_samples_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bandwidth_samples
    ADD CONSTRAINT bandwidth_samples_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: bank_accounts bank_accounts_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_accounts
    ADD CONSTRAINT bank_accounts_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.subscribers(id);


--
-- Name: bank_accounts bank_accounts_payment_method_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_accounts
    ADD CONSTRAINT bank_accounts_payment_method_id_fkey FOREIGN KEY (payment_method_id) REFERENCES public.payment_methods(id);


--
-- Name: bank_reconciliation_items bank_reconciliation_items_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_items
    ADD CONSTRAINT bank_reconciliation_items_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.bank_reconciliation_runs(id);


--
-- Name: buildout_milestones buildout_milestones_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_milestones
    ADD CONSTRAINT buildout_milestones_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.buildout_projects(id);


--
-- Name: buildout_projects buildout_projects_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_projects
    ADD CONSTRAINT buildout_projects_address_id_fkey FOREIGN KEY (address_id) REFERENCES public.addresses(id);


--
-- Name: buildout_projects buildout_projects_coverage_area_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_projects
    ADD CONSTRAINT buildout_projects_coverage_area_id_fkey FOREIGN KEY (coverage_area_id) REFERENCES public.coverage_areas(id);


--
-- Name: buildout_projects buildout_projects_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_projects
    ADD CONSTRAINT buildout_projects_request_id_fkey FOREIGN KEY (request_id) REFERENCES public.buildout_requests(id);


--
-- Name: buildout_requests buildout_requests_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_requests
    ADD CONSTRAINT buildout_requests_address_id_fkey FOREIGN KEY (address_id) REFERENCES public.addresses(id);


--
-- Name: buildout_requests buildout_requests_coverage_area_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_requests
    ADD CONSTRAINT buildout_requests_coverage_area_id_fkey FOREIGN KEY (coverage_area_id) REFERENCES public.coverage_areas(id);


--
-- Name: buildout_requests buildout_requests_qualification_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_requests
    ADD CONSTRAINT buildout_requests_qualification_id_fkey FOREIGN KEY (qualification_id) REFERENCES public.service_qualifications(id);


--
-- Name: buildout_updates buildout_updates_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildout_updates
    ADD CONSTRAINT buildout_updates_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.buildout_projects(id);


--
-- Name: catalog_offers catalog_offers_default_ont_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_offers
    ADD CONSTRAINT catalog_offers_default_ont_profile_id_fkey FOREIGN KEY (default_ont_profile_id) REFERENCES public.ont_provisioning_profiles(id);


--
-- Name: catalog_offers catalog_offers_policy_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_offers
    ADD CONSTRAINT catalog_offers_policy_set_id_fkey FOREIGN KEY (policy_set_id) REFERENCES public.policy_sets(id);


--
-- Name: catalog_offers catalog_offers_region_zone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_offers
    ADD CONSTRAINT catalog_offers_region_zone_id_fkey FOREIGN KEY (region_zone_id) REFERENCES public.region_zones(id);


--
-- Name: catalog_offers catalog_offers_sla_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_offers
    ADD CONSTRAINT catalog_offers_sla_profile_id_fkey FOREIGN KEY (sla_profile_id) REFERENCES public.sla_profiles(id);


--
-- Name: catalog_offers catalog_offers_usage_allowance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_offers
    ADD CONSTRAINT catalog_offers_usage_allowance_id_fkey FOREIGN KEY (usage_allowance_id) REFERENCES public.usage_allowances(id);


--
-- Name: communication_logs communication_logs_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.communication_logs
    ADD CONSTRAINT communication_logs_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: communication_logs communication_logs_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.communication_logs
    ADD CONSTRAINT communication_logs_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: contract_signatures contract_signatures_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contract_signatures
    ADD CONSTRAINT contract_signatures_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.legal_documents(id);


--
-- Name: contract_signatures contract_signatures_service_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contract_signatures
    ADD CONSTRAINT contract_signatures_service_order_id_fkey FOREIGN KEY (service_order_id) REFERENCES public.service_orders(id);


--
-- Name: contract_signatures contract_signatures_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contract_signatures
    ADD CONSTRAINT contract_signatures_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: cpe_devices cpe_devices_service_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cpe_devices
    ADD CONSTRAINT cpe_devices_service_address_id_fkey FOREIGN KEY (service_address_id) REFERENCES public.addresses(id);


--
-- Name: cpe_devices cpe_devices_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cpe_devices
    ADD CONSTRAINT cpe_devices_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: cpe_devices cpe_devices_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cpe_devices
    ADD CONSTRAINT cpe_devices_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: credit_note_applications credit_note_applications_credit_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_note_applications
    ADD CONSTRAINT credit_note_applications_credit_note_id_fkey FOREIGN KEY (credit_note_id) REFERENCES public.credit_notes(id);


--
-- Name: credit_note_applications credit_note_applications_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_note_applications
    ADD CONSTRAINT credit_note_applications_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: credit_note_lines credit_note_lines_credit_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_note_lines
    ADD CONSTRAINT credit_note_lines_credit_note_id_fkey FOREIGN KEY (credit_note_id) REFERENCES public.credit_notes(id);


--
-- Name: credit_note_lines credit_note_lines_tax_rate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_note_lines
    ADD CONSTRAINT credit_note_lines_tax_rate_id_fkey FOREIGN KEY (tax_rate_id) REFERENCES public.tax_rates(id);


--
-- Name: credit_notes credit_notes_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_notes
    ADD CONSTRAINT credit_notes_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.subscribers(id);


--
-- Name: credit_notes credit_notes_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_notes
    ADD CONSTRAINT credit_notes_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: device_interfaces device_interfaces_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_interfaces
    ADD CONSTRAINT device_interfaces_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: device_metrics device_metrics_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_metrics
    ADD CONSTRAINT device_metrics_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: device_metrics device_metrics_interface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_metrics
    ADD CONSTRAINT device_metrics_interface_id_fkey FOREIGN KEY (interface_id) REFERENCES public.device_interfaces(id);


--
-- Name: dns_threat_events dns_threat_events_network_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dns_threat_events
    ADD CONSTRAINT dns_threat_events_network_device_id_fkey FOREIGN KEY (network_device_id) REFERENCES public.network_devices(id);


--
-- Name: dns_threat_events dns_threat_events_pop_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dns_threat_events
    ADD CONSTRAINT dns_threat_events_pop_site_id_fkey FOREIGN KEY (pop_site_id) REFERENCES public.pop_sites(id);


--
-- Name: dns_threat_events dns_threat_events_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dns_threat_events
    ADD CONSTRAINT dns_threat_events_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: dunning_action_logs dunning_action_logs_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dunning_action_logs
    ADD CONSTRAINT dunning_action_logs_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.dunning_cases(id);


--
-- Name: dunning_action_logs dunning_action_logs_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dunning_action_logs
    ADD CONSTRAINT dunning_action_logs_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: dunning_action_logs dunning_action_logs_payment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dunning_action_logs
    ADD CONSTRAINT dunning_action_logs_payment_id_fkey FOREIGN KEY (payment_id) REFERENCES public.payments(id);


--
-- Name: dunning_cases dunning_cases_policy_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dunning_cases
    ADD CONSTRAINT dunning_cases_policy_set_id_fkey FOREIGN KEY (policy_set_id) REFERENCES public.policy_sets(id);


--
-- Name: dunning_cases dunning_cases_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dunning_cases
    ADD CONSTRAINT dunning_cases_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: enforcement_locks enforcement_locks_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enforcement_locks
    ADD CONSTRAINT enforcement_locks_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id) ON DELETE CASCADE;


--
-- Name: enforcement_locks enforcement_locks_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enforcement_locks
    ADD CONSTRAINT enforcement_locks_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id) ON DELETE CASCADE;


--
-- Name: external_references external_references_connector_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_references
    ADD CONSTRAINT external_references_connector_config_id_fkey FOREIGN KEY (connector_config_id) REFERENCES public.connector_configs(id);


--
-- Name: fdh_cabinets fdh_cabinets_region_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fdh_cabinets
    ADD CONSTRAINT fdh_cabinets_region_id_fkey FOREIGN KEY (region_id) REFERENCES public.region_zones(id);


--
-- Name: fdh_cabinets fdh_cabinets_zone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fdh_cabinets
    ADD CONSTRAINT fdh_cabinets_zone_id_fkey FOREIGN KEY (zone_id) REFERENCES public.network_zones(id);


--
-- Name: fiber_change_requests fiber_change_requests_requested_by_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_change_requests
    ADD CONSTRAINT fiber_change_requests_requested_by_person_id_fkey FOREIGN KEY (requested_by_person_id) REFERENCES public.subscribers(id);


--
-- Name: fiber_change_requests fiber_change_requests_reviewed_by_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_change_requests
    ADD CONSTRAINT fiber_change_requests_reviewed_by_person_id_fkey FOREIGN KEY (reviewed_by_person_id) REFERENCES public.subscribers(id);


--
-- Name: fiber_segments fiber_segments_fiber_strand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_segments
    ADD CONSTRAINT fiber_segments_fiber_strand_id_fkey FOREIGN KEY (fiber_strand_id) REFERENCES public.fiber_strands(id);


--
-- Name: fiber_segments fiber_segments_from_point_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_segments
    ADD CONSTRAINT fiber_segments_from_point_id_fkey FOREIGN KEY (from_point_id) REFERENCES public.fiber_termination_points(id);


--
-- Name: fiber_segments fiber_segments_to_point_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_segments
    ADD CONSTRAINT fiber_segments_to_point_id_fkey FOREIGN KEY (to_point_id) REFERENCES public.fiber_termination_points(id);


--
-- Name: fiber_splice_trays fiber_splice_trays_closure_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splice_trays
    ADD CONSTRAINT fiber_splice_trays_closure_id_fkey FOREIGN KEY (closure_id) REFERENCES public.fiber_splice_closures(id);


--
-- Name: fiber_splices fiber_splices_closure_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splices
    ADD CONSTRAINT fiber_splices_closure_id_fkey FOREIGN KEY (closure_id) REFERENCES public.fiber_splice_closures(id);


--
-- Name: fiber_splices fiber_splices_from_strand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splices
    ADD CONSTRAINT fiber_splices_from_strand_id_fkey FOREIGN KEY (from_strand_id) REFERENCES public.fiber_strands(id);


--
-- Name: fiber_splices fiber_splices_to_strand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splices
    ADD CONSTRAINT fiber_splices_to_strand_id_fkey FOREIGN KEY (to_strand_id) REFERENCES public.fiber_strands(id);


--
-- Name: fiber_splices fiber_splices_tray_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiber_splices
    ADD CONSTRAINT fiber_splices_tray_id_fkey FOREIGN KEY (tray_id) REFERENCES public.fiber_splice_trays(id);


--
-- Name: fup_policies fup_policies_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_policies
    ADD CONSTRAINT fup_policies_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: fup_rules fup_rules_enabled_by_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_rules
    ADD CONSTRAINT fup_rules_enabled_by_rule_id_fkey FOREIGN KEY (enabled_by_rule_id) REFERENCES public.fup_rules(id) ON DELETE SET NULL;


--
-- Name: fup_rules fup_rules_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_rules
    ADD CONSTRAINT fup_rules_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES public.fup_policies(id) ON DELETE CASCADE;


--
-- Name: fup_states fup_states_active_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_states
    ADD CONSTRAINT fup_states_active_rule_id_fkey FOREIGN KEY (active_rule_id) REFERENCES public.fup_rules(id) ON DELETE SET NULL;


--
-- Name: fup_states fup_states_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_states
    ADD CONSTRAINT fup_states_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: fup_states fup_states_original_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_states
    ADD CONSTRAINT fup_states_original_profile_id_fkey FOREIGN KEY (original_profile_id) REFERENCES public.radius_profiles(id) ON DELETE SET NULL;


--
-- Name: fup_states fup_states_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_states
    ADD CONSTRAINT fup_states_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id) ON DELETE CASCADE;


--
-- Name: fup_states fup_states_throttle_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fup_states
    ADD CONSTRAINT fup_states_throttle_profile_id_fkey FOREIGN KEY (throttle_profile_id) REFERENCES public.radius_profiles(id) ON DELETE SET NULL;


--
-- Name: geo_locations geo_locations_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geo_locations
    ADD CONSTRAINT geo_locations_address_id_fkey FOREIGN KEY (address_id) REFERENCES public.addresses(id);


--
-- Name: geo_locations geo_locations_pop_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.geo_locations
    ADD CONSTRAINT geo_locations_pop_site_id_fkey FOREIGN KEY (pop_site_id) REFERENCES public.pop_sites(id);


--
-- Name: install_appointments install_appointments_service_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.install_appointments
    ADD CONSTRAINT install_appointments_service_order_id_fkey FOREIGN KEY (service_order_id) REFERENCES public.service_orders(id);


--
-- Name: integration_hook_executions integration_hook_executions_hook_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_hook_executions
    ADD CONSTRAINT integration_hook_executions_hook_id_fkey FOREIGN KEY (hook_id) REFERENCES public.integration_hooks(id) ON DELETE CASCADE;


--
-- Name: integration_jobs integration_jobs_target_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_jobs
    ADD CONSTRAINT integration_jobs_target_id_fkey FOREIGN KEY (target_id) REFERENCES public.integration_targets(id);


--
-- Name: integration_runs integration_runs_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_runs
    ADD CONSTRAINT integration_runs_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.integration_jobs(id);


--
-- Name: integration_targets integration_targets_connector_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_targets
    ADD CONSTRAINT integration_targets_connector_config_id_fkey FOREIGN KEY (connector_config_id) REFERENCES public.connector_configs(id);


--
-- Name: invoice_lines invoice_lines_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: invoice_lines invoice_lines_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: invoice_lines invoice_lines_tax_rate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_tax_rate_id_fkey FOREIGN KEY (tax_rate_id) REFERENCES public.tax_rates(id);


--
-- Name: invoice_pdf_exports invoice_pdf_exports_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_pdf_exports
    ADD CONSTRAINT invoice_pdf_exports_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: invoice_pdf_exports invoice_pdf_exports_requested_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_pdf_exports
    ADD CONSTRAINT invoice_pdf_exports_requested_by_id_fkey FOREIGN KEY (requested_by_id) REFERENCES public.subscribers(id);


--
-- Name: invoices invoices_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.subscribers(id);


--
-- Name: invoices invoices_added_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_added_by_id_fkey FOREIGN KEY (added_by_id) REFERENCES public.subscribers(id);


--
-- Name: ip_assignments ip_assignments_ipv4_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT ip_assignments_ipv4_address_id_fkey FOREIGN KEY (ipv4_address_id) REFERENCES public.ipv4_addresses(id);


--
-- Name: ip_assignments ip_assignments_ipv6_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT ip_assignments_ipv6_address_id_fkey FOREIGN KEY (ipv6_address_id) REFERENCES public.ipv6_addresses(id);


--
-- Name: ip_assignments ip_assignments_service_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT ip_assignments_service_address_id_fkey FOREIGN KEY (service_address_id) REFERENCES public.addresses(id);


--
-- Name: ip_assignments ip_assignments_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT ip_assignments_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: ip_assignments ip_assignments_subscription_add_on_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT ip_assignments_subscription_add_on_id_fkey FOREIGN KEY (subscription_add_on_id) REFERENCES public.subscription_add_ons(id);


--
-- Name: ip_assignments ip_assignments_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_assignments
    ADD CONSTRAINT ip_assignments_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: ip_blocks ip_blocks_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_blocks
    ADD CONSTRAINT ip_blocks_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.ip_pools(id);


--
-- Name: ip_pools ip_pools_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_pools
    ADD CONSTRAINT ip_pools_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id) ON DELETE SET NULL;


--
-- Name: ip_pools ip_pools_olt_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ip_pools
    ADD CONSTRAINT ip_pools_olt_device_id_fkey FOREIGN KEY (olt_device_id) REFERENCES public.olt_devices(id) ON DELETE SET NULL;


--
-- Name: ipv4_addresses ipv4_addresses_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ipv4_addresses
    ADD CONSTRAINT ipv4_addresses_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.ip_pools(id);


--
-- Name: ipv6_addresses ipv6_addresses_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ipv6_addresses
    ADD CONSTRAINT ipv6_addresses_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.ip_pools(id);


--
-- Name: ledger_entries ledger_entries_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ledger_entries
    ADD CONSTRAINT ledger_entries_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.subscribers(id);


--
-- Name: ledger_entries ledger_entries_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ledger_entries
    ADD CONSTRAINT ledger_entries_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: ledger_entries ledger_entries_payment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ledger_entries
    ADD CONSTRAINT ledger_entries_payment_id_fkey FOREIGN KEY (payment_id) REFERENCES public.payments(id);


--
-- Name: mfa_methods mfa_methods_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_methods
    ADD CONSTRAINT mfa_methods_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: mfa_methods mfa_methods_system_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_methods
    ADD CONSTRAINT mfa_methods_system_user_id_fkey FOREIGN KEY (system_user_id) REFERENCES public.system_users(id);


--
-- Name: mrr_snapshots mrr_snapshots_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mrr_snapshots
    ADD CONSTRAINT mrr_snapshots_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id) ON DELETE CASCADE;


--
-- Name: nas_config_backups nas_config_backups_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_config_backups
    ADD CONSTRAINT nas_config_backups_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: nas_connection_rules nas_connection_rules_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_connection_rules
    ADD CONSTRAINT nas_connection_rules_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: nas_devices nas_devices_network_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_devices
    ADD CONSTRAINT nas_devices_network_device_id_fkey FOREIGN KEY (network_device_id) REFERENCES public.network_devices(id);


--
-- Name: nas_devices nas_devices_pop_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nas_devices
    ADD CONSTRAINT nas_devices_pop_site_id_fkey FOREIGN KEY (pop_site_id) REFERENCES public.pop_sites(id);


--
-- Name: network_device_bandwidth_graph_sources network_device_bandwidth_graph_sources_graph_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_bandwidth_graph_sources
    ADD CONSTRAINT network_device_bandwidth_graph_sources_graph_id_fkey FOREIGN KEY (graph_id) REFERENCES public.network_device_bandwidth_graphs(id);


--
-- Name: network_device_bandwidth_graph_sources network_device_bandwidth_graph_sources_snmp_oid_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_bandwidth_graph_sources
    ADD CONSTRAINT network_device_bandwidth_graph_sources_snmp_oid_id_fkey FOREIGN KEY (snmp_oid_id) REFERENCES public.network_device_snmp_oids(id);


--
-- Name: network_device_bandwidth_graph_sources network_device_bandwidth_graph_sources_source_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_bandwidth_graph_sources
    ADD CONSTRAINT network_device_bandwidth_graph_sources_source_device_id_fkey FOREIGN KEY (source_device_id) REFERENCES public.network_devices(id);


--
-- Name: network_device_bandwidth_graphs network_device_bandwidth_graphs_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_bandwidth_graphs
    ADD CONSTRAINT network_device_bandwidth_graphs_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: network_device_snmp_oids network_device_snmp_oids_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_device_snmp_oids
    ADD CONSTRAINT network_device_snmp_oids_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: network_devices network_devices_parent_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_devices
    ADD CONSTRAINT network_devices_parent_device_id_fkey FOREIGN KEY (parent_device_id) REFERENCES public.network_devices(id) ON DELETE SET NULL;


--
-- Name: network_devices network_devices_pop_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_devices
    ADD CONSTRAINT network_devices_pop_site_id_fkey FOREIGN KEY (pop_site_id) REFERENCES public.pop_sites(id);


--
-- Name: network_operations network_operations_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_operations
    ADD CONSTRAINT network_operations_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.network_operations(id) ON DELETE CASCADE;


--
-- Name: network_topology_links network_topology_links_source_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_topology_links
    ADD CONSTRAINT network_topology_links_source_device_id_fkey FOREIGN KEY (source_device_id) REFERENCES public.network_devices(id);


--
-- Name: network_topology_links network_topology_links_source_interface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_topology_links
    ADD CONSTRAINT network_topology_links_source_interface_id_fkey FOREIGN KEY (source_interface_id) REFERENCES public.device_interfaces(id);


--
-- Name: network_topology_links network_topology_links_target_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_topology_links
    ADD CONSTRAINT network_topology_links_target_device_id_fkey FOREIGN KEY (target_device_id) REFERENCES public.network_devices(id);


--
-- Name: network_topology_links network_topology_links_target_interface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_topology_links
    ADD CONSTRAINT network_topology_links_target_interface_id_fkey FOREIGN KEY (target_interface_id) REFERENCES public.device_interfaces(id);


--
-- Name: network_zones network_zones_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_zones
    ADD CONSTRAINT network_zones_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.network_zones(id);


--
-- Name: notification_deliveries notification_deliveries_notification_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.notifications(id);


--
-- Name: notifications notifications_connector_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_connector_config_id_fkey FOREIGN KEY (connector_config_id) REFERENCES public.connector_configs(id);


--
-- Name: notifications notifications_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.notification_templates(id);


--
-- Name: oauth_tokens oauth_tokens_connector_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT oauth_tokens_connector_config_id_fkey FOREIGN KEY (connector_config_id) REFERENCES public.connector_configs(id);


--
-- Name: offer_add_ons offer_add_ons_add_on_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_add_ons
    ADD CONSTRAINT offer_add_ons_add_on_id_fkey FOREIGN KEY (add_on_id) REFERENCES public.add_ons(id);


--
-- Name: offer_add_ons offer_add_ons_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_add_ons
    ADD CONSTRAINT offer_add_ons_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: offer_billing_mode_availability offer_billing_mode_availability_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_billing_mode_availability
    ADD CONSTRAINT offer_billing_mode_availability_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id) ON DELETE CASCADE;


--
-- Name: offer_category_availability offer_category_availability_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_category_availability
    ADD CONSTRAINT offer_category_availability_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id) ON DELETE CASCADE;


--
-- Name: offer_location_availability offer_location_availability_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_location_availability
    ADD CONSTRAINT offer_location_availability_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id) ON DELETE CASCADE;


--
-- Name: offer_location_availability offer_location_availability_pop_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_location_availability
    ADD CONSTRAINT offer_location_availability_pop_site_id_fkey FOREIGN KEY (pop_site_id) REFERENCES public.pop_sites(id) ON DELETE CASCADE;


--
-- Name: offer_prices offer_prices_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_prices
    ADD CONSTRAINT offer_prices_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: offer_radius_profiles offer_radius_profiles_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_radius_profiles
    ADD CONSTRAINT offer_radius_profiles_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: offer_radius_profiles offer_radius_profiles_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_radius_profiles
    ADD CONSTRAINT offer_radius_profiles_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.radius_profiles(id);


--
-- Name: offer_reseller_availability offer_reseller_availability_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_reseller_availability
    ADD CONSTRAINT offer_reseller_availability_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id) ON DELETE CASCADE;


--
-- Name: offer_reseller_availability offer_reseller_availability_reseller_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_reseller_availability
    ADD CONSTRAINT offer_reseller_availability_reseller_id_fkey FOREIGN KEY (reseller_id) REFERENCES public.resellers(id) ON DELETE CASCADE;


--
-- Name: offer_version_prices offer_version_prices_offer_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_version_prices
    ADD CONSTRAINT offer_version_prices_offer_version_id_fkey FOREIGN KEY (offer_version_id) REFERENCES public.offer_versions(id);


--
-- Name: offer_versions offer_versions_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_versions
    ADD CONSTRAINT offer_versions_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: offer_versions offer_versions_policy_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_versions
    ADD CONSTRAINT offer_versions_policy_set_id_fkey FOREIGN KEY (policy_set_id) REFERENCES public.policy_sets(id);


--
-- Name: offer_versions offer_versions_region_zone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_versions
    ADD CONSTRAINT offer_versions_region_zone_id_fkey FOREIGN KEY (region_zone_id) REFERENCES public.region_zones(id);


--
-- Name: offer_versions offer_versions_sla_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_versions
    ADD CONSTRAINT offer_versions_sla_profile_id_fkey FOREIGN KEY (sla_profile_id) REFERENCES public.sla_profiles(id);


--
-- Name: offer_versions offer_versions_usage_allowance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_versions
    ADD CONSTRAINT offer_versions_usage_allowance_id_fkey FOREIGN KEY (usage_allowance_id) REFERENCES public.usage_allowances(id);


--
-- Name: olt_autofind_candidates olt_autofind_candidates_olt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_autofind_candidates
    ADD CONSTRAINT olt_autofind_candidates_olt_id_fkey FOREIGN KEY (olt_id) REFERENCES public.olt_devices(id) ON DELETE CASCADE;


--
-- Name: olt_autofind_candidates olt_autofind_candidates_ont_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_autofind_candidates
    ADD CONSTRAINT olt_autofind_candidates_ont_unit_id_fkey FOREIGN KEY (ont_unit_id) REFERENCES public.ont_units(id) ON DELETE SET NULL;


--
-- Name: olt_card_ports olt_card_ports_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_card_ports
    ADD CONSTRAINT olt_card_ports_card_id_fkey FOREIGN KEY (card_id) REFERENCES public.olt_cards(id);


--
-- Name: olt_cards olt_cards_shelf_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_cards
    ADD CONSTRAINT olt_cards_shelf_id_fkey FOREIGN KEY (shelf_id) REFERENCES public.olt_shelves(id);


--
-- Name: olt_config_backups olt_config_backups_olt_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_config_backups
    ADD CONSTRAINT olt_config_backups_olt_device_id_fkey FOREIGN KEY (olt_device_id) REFERENCES public.olt_devices(id);


--
-- Name: olt_devices olt_devices_tr069_acs_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_devices
    ADD CONSTRAINT olt_devices_tr069_acs_server_id_fkey FOREIGN KEY (tr069_acs_server_id) REFERENCES public.tr069_acs_servers(id);


--
-- Name: olt_power_units olt_power_units_olt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_power_units
    ADD CONSTRAINT olt_power_units_olt_id_fkey FOREIGN KEY (olt_id) REFERENCES public.olt_devices(id);


--
-- Name: olt_sfp_modules olt_sfp_modules_olt_card_port_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_sfp_modules
    ADD CONSTRAINT olt_sfp_modules_olt_card_port_id_fkey FOREIGN KEY (olt_card_port_id) REFERENCES public.olt_card_ports(id);


--
-- Name: olt_shelves olt_shelves_olt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.olt_shelves
    ADD CONSTRAINT olt_shelves_olt_id_fkey FOREIGN KEY (olt_id) REFERENCES public.olt_devices(id);


--
-- Name: on_call_rotation_members on_call_rotation_members_rotation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.on_call_rotation_members
    ADD CONSTRAINT on_call_rotation_members_rotation_id_fkey FOREIGN KEY (rotation_id) REFERENCES public.on_call_rotations(id);


--
-- Name: ont_assignments ont_assignments_ont_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_assignments
    ADD CONSTRAINT ont_assignments_ont_unit_id_fkey FOREIGN KEY (ont_unit_id) REFERENCES public.ont_units(id);


--
-- Name: ont_assignments ont_assignments_pon_port_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_assignments
    ADD CONSTRAINT ont_assignments_pon_port_id_fkey FOREIGN KEY (pon_port_id) REFERENCES public.pon_ports(id);


--
-- Name: ont_assignments ont_assignments_service_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_assignments
    ADD CONSTRAINT ont_assignments_service_address_id_fkey FOREIGN KEY (service_address_id) REFERENCES public.addresses(id);


--
-- Name: ont_assignments ont_assignments_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_assignments
    ADD CONSTRAINT ont_assignments_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: ont_assignments ont_assignments_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_assignments
    ADD CONSTRAINT ont_assignments_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: ont_profile_wan_services ont_profile_wan_services_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_profile_wan_services
    ADD CONSTRAINT ont_profile_wan_services_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.ont_provisioning_profiles(id) ON DELETE CASCADE;


--
-- Name: ont_provisioning_profiles ont_provisioning_profiles_download_speed_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_provisioning_profiles
    ADD CONSTRAINT ont_provisioning_profiles_download_speed_profile_id_fkey FOREIGN KEY (download_speed_profile_id) REFERENCES public.speed_profiles(id);


--
-- Name: ont_provisioning_profiles ont_provisioning_profiles_owner_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_provisioning_profiles
    ADD CONSTRAINT ont_provisioning_profiles_owner_subscriber_id_fkey FOREIGN KEY (owner_subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: ont_provisioning_profiles ont_provisioning_profiles_upload_speed_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_provisioning_profiles
    ADD CONSTRAINT ont_provisioning_profiles_upload_speed_profile_id_fkey FOREIGN KEY (upload_speed_profile_id) REFERENCES public.speed_profiles(id);


--
-- Name: ont_units ont_units_download_speed_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_download_speed_profile_id_fkey FOREIGN KEY (download_speed_profile_id) REFERENCES public.speed_profiles(id);


--
-- Name: ont_units ont_units_mgmt_vlan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_mgmt_vlan_id_fkey FOREIGN KEY (mgmt_vlan_id) REFERENCES public.vlans(id);


--
-- Name: ont_units ont_units_olt_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_olt_device_id_fkey FOREIGN KEY (olt_device_id) REFERENCES public.olt_devices(id);


--
-- Name: ont_units ont_units_onu_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_onu_type_id_fkey FOREIGN KEY (onu_type_id) REFERENCES public.onu_types(id);


--
-- Name: ont_units ont_units_provisioning_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_provisioning_profile_id_fkey FOREIGN KEY (provisioning_profile_id) REFERENCES public.ont_provisioning_profiles(id);


--
-- Name: ont_units ont_units_splitter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_splitter_id_fkey FOREIGN KEY (splitter_id) REFERENCES public.splitters(id);


--
-- Name: ont_units ont_units_splitter_port_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_splitter_port_id_fkey FOREIGN KEY (splitter_port_id) REFERENCES public.splitter_ports(id);


--
-- Name: ont_units ont_units_tr069_acs_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_tr069_acs_server_id_fkey FOREIGN KEY (tr069_acs_server_id) REFERENCES public.tr069_acs_servers(id);


--
-- Name: ont_units ont_units_upload_speed_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_upload_speed_profile_id_fkey FOREIGN KEY (upload_speed_profile_id) REFERENCES public.speed_profiles(id);


--
-- Name: ont_units ont_units_user_vlan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_user_vlan_id_fkey FOREIGN KEY (user_vlan_id) REFERENCES public.vlans(id);


--
-- Name: ont_units ont_units_wan_vlan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_wan_vlan_id_fkey FOREIGN KEY (wan_vlan_id) REFERENCES public.vlans(id);


--
-- Name: ont_units ont_units_zone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ont_units
    ADD CONSTRAINT ont_units_zone_id_fkey FOREIGN KEY (zone_id) REFERENCES public.network_zones(id);


--
-- Name: payment_allocations payment_allocations_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocations
    ADD CONSTRAINT payment_allocations_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: payment_allocations payment_allocations_payment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocations
    ADD CONSTRAINT payment_allocations_payment_id_fkey FOREIGN KEY (payment_id) REFERENCES public.payments(id);


--
-- Name: payment_arrangement_installments payment_arrangement_installments_arrangement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_arrangement_installments
    ADD CONSTRAINT payment_arrangement_installments_arrangement_id_fkey FOREIGN KEY (arrangement_id) REFERENCES public.payment_arrangements(id);


--
-- Name: payment_arrangement_installments payment_arrangement_installments_payment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_arrangement_installments
    ADD CONSTRAINT payment_arrangement_installments_payment_id_fkey FOREIGN KEY (payment_id) REFERENCES public.payments(id);


--
-- Name: payment_arrangements payment_arrangements_approved_by_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_arrangements
    ADD CONSTRAINT payment_arrangements_approved_by_subscriber_id_fkey FOREIGN KEY (approved_by_subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: payment_arrangements payment_arrangements_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_arrangements
    ADD CONSTRAINT payment_arrangements_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: payment_arrangements payment_arrangements_requested_by_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_arrangements
    ADD CONSTRAINT payment_arrangements_requested_by_subscriber_id_fkey FOREIGN KEY (requested_by_subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: payment_arrangements payment_arrangements_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_arrangements
    ADD CONSTRAINT payment_arrangements_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: payment_channel_accounts payment_channel_accounts_channel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_channel_accounts
    ADD CONSTRAINT payment_channel_accounts_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.payment_channels(id);


--
-- Name: payment_channel_accounts payment_channel_accounts_collection_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_channel_accounts
    ADD CONSTRAINT payment_channel_accounts_collection_account_id_fkey FOREIGN KEY (collection_account_id) REFERENCES public.collection_accounts(id);


--
-- Name: payment_channels payment_channels_default_collection_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_channels
    ADD CONSTRAINT payment_channels_default_collection_account_id_fkey FOREIGN KEY (default_collection_account_id) REFERENCES public.collection_accounts(id);


--
-- Name: payment_channels payment_channels_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_channels
    ADD CONSTRAINT payment_channels_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.payment_providers(id);


--
-- Name: payment_methods payment_methods_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_methods
    ADD CONSTRAINT payment_methods_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.subscribers(id);


--
-- Name: payment_methods payment_methods_payment_channel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_methods
    ADD CONSTRAINT payment_methods_payment_channel_id_fkey FOREIGN KEY (payment_channel_id) REFERENCES public.payment_channels(id);


--
-- Name: payment_provider_events payment_provider_events_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_provider_events
    ADD CONSTRAINT payment_provider_events_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id);


--
-- Name: payment_provider_events payment_provider_events_payment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_provider_events
    ADD CONSTRAINT payment_provider_events_payment_id_fkey FOREIGN KEY (payment_id) REFERENCES public.payments(id);


--
-- Name: payment_provider_events payment_provider_events_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_provider_events
    ADD CONSTRAINT payment_provider_events_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.payment_providers(id);


--
-- Name: payment_providers payment_providers_connector_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_providers
    ADD CONSTRAINT payment_providers_connector_config_id_fkey FOREIGN KEY (connector_config_id) REFERENCES public.connector_configs(id);


--
-- Name: payments payments_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.subscribers(id);


--
-- Name: payments payments_collection_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_collection_account_id_fkey FOREIGN KEY (collection_account_id) REFERENCES public.collection_accounts(id);


--
-- Name: payments payments_payment_channel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_payment_channel_id_fkey FOREIGN KEY (payment_channel_id) REFERENCES public.payment_channels(id);


--
-- Name: payments payments_payment_method_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_payment_method_id_fkey FOREIGN KEY (payment_method_id) REFERENCES public.payment_methods(id);


--
-- Name: payments payments_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.payment_providers(id);


--
-- Name: policy_dunning_steps policy_dunning_steps_policy_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_dunning_steps
    ADD CONSTRAINT policy_dunning_steps_policy_set_id_fkey FOREIGN KEY (policy_set_id) REFERENCES public.policy_sets(id);


--
-- Name: pon_port_splitter_links pon_port_splitter_links_pon_port_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pon_port_splitter_links
    ADD CONSTRAINT pon_port_splitter_links_pon_port_id_fkey FOREIGN KEY (pon_port_id) REFERENCES public.pon_ports(id);


--
-- Name: pon_port_splitter_links pon_port_splitter_links_splitter_port_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pon_port_splitter_links
    ADD CONSTRAINT pon_port_splitter_links_splitter_port_id_fkey FOREIGN KEY (splitter_port_id) REFERENCES public.splitter_ports(id);


--
-- Name: pon_ports pon_ports_olt_card_port_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pon_ports
    ADD CONSTRAINT pon_ports_olt_card_port_id_fkey FOREIGN KEY (olt_card_port_id) REFERENCES public.olt_card_ports(id);


--
-- Name: pon_ports pon_ports_olt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pon_ports
    ADD CONSTRAINT pon_ports_olt_id_fkey FOREIGN KEY (olt_id) REFERENCES public.olt_devices(id);


--
-- Name: pop_site_contacts pop_site_contacts_pop_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pop_site_contacts
    ADD CONSTRAINT pop_site_contacts_pop_site_id_fkey FOREIGN KEY (pop_site_id) REFERENCES public.pop_sites(id);


--
-- Name: pop_sites pop_sites_owner_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pop_sites
    ADD CONSTRAINT pop_sites_owner_subscriber_id_fkey FOREIGN KEY (owner_subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: pop_sites pop_sites_reseller_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pop_sites
    ADD CONSTRAINT pop_sites_reseller_id_fkey FOREIGN KEY (reseller_id) REFERENCES public.resellers(id);


--
-- Name: pop_sites pop_sites_zone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pop_sites
    ADD CONSTRAINT pop_sites_zone_id_fkey FOREIGN KEY (zone_id) REFERENCES public.network_zones(id);


--
-- Name: port_vlans port_vlans_port_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.port_vlans
    ADD CONSTRAINT port_vlans_port_id_fkey FOREIGN KEY (port_id) REFERENCES public.ports(id);


--
-- Name: port_vlans port_vlans_vlan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.port_vlans
    ADD CONSTRAINT port_vlans_vlan_id_fkey FOREIGN KEY (vlan_id) REFERENCES public.vlans(id);


--
-- Name: portal_messages portal_messages_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_messages
    ADD CONSTRAINT portal_messages_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id) ON DELETE CASCADE;


--
-- Name: portal_onboarding_states portal_onboarding_states_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_onboarding_states
    ADD CONSTRAINT portal_onboarding_states_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id) ON DELETE CASCADE;


--
-- Name: provisioning_logs provisioning_logs_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_logs
    ADD CONSTRAINT provisioning_logs_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: provisioning_logs provisioning_logs_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_logs
    ADD CONSTRAINT provisioning_logs_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: provisioning_logs provisioning_logs_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_logs
    ADD CONSTRAINT provisioning_logs_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: provisioning_logs provisioning_logs_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_logs
    ADD CONSTRAINT provisioning_logs_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.provisioning_templates(id);


--
-- Name: provisioning_runs provisioning_runs_service_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_runs
    ADD CONSTRAINT provisioning_runs_service_order_id_fkey FOREIGN KEY (service_order_id) REFERENCES public.service_orders(id);


--
-- Name: provisioning_runs provisioning_runs_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_runs
    ADD CONSTRAINT provisioning_runs_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: provisioning_runs provisioning_runs_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_runs
    ADD CONSTRAINT provisioning_runs_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.provisioning_workflows(id);


--
-- Name: provisioning_steps provisioning_steps_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_steps
    ADD CONSTRAINT provisioning_steps_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.provisioning_workflows(id);


--
-- Name: provisioning_tasks provisioning_tasks_service_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provisioning_tasks
    ADD CONSTRAINT provisioning_tasks_service_order_id_fkey FOREIGN KEY (service_order_id) REFERENCES public.service_orders(id);


--
-- Name: queue_mappings queue_mappings_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.queue_mappings
    ADD CONSTRAINT queue_mappings_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: queue_mappings queue_mappings_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.queue_mappings
    ADD CONSTRAINT queue_mappings_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: quota_buckets quota_buckets_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quota_buckets
    ADD CONSTRAINT quota_buckets_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: radius_accounting_sessions radius_accounting_sessions_access_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_accounting_sessions
    ADD CONSTRAINT radius_accounting_sessions_access_credential_id_fkey FOREIGN KEY (access_credential_id) REFERENCES public.access_credentials(id);


--
-- Name: radius_accounting_sessions radius_accounting_sessions_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_accounting_sessions
    ADD CONSTRAINT radius_accounting_sessions_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: radius_accounting_sessions radius_accounting_sessions_radius_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_accounting_sessions
    ADD CONSTRAINT radius_accounting_sessions_radius_client_id_fkey FOREIGN KEY (radius_client_id) REFERENCES public.radius_clients(id);


--
-- Name: radius_accounting_sessions radius_accounting_sessions_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_accounting_sessions
    ADD CONSTRAINT radius_accounting_sessions_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: radius_active_sessions radius_active_sessions_access_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_active_sessions
    ADD CONSTRAINT radius_active_sessions_access_credential_id_fkey FOREIGN KEY (access_credential_id) REFERENCES public.access_credentials(id);


--
-- Name: radius_active_sessions radius_active_sessions_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_active_sessions
    ADD CONSTRAINT radius_active_sessions_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: radius_active_sessions radius_active_sessions_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_active_sessions
    ADD CONSTRAINT radius_active_sessions_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: radius_active_sessions radius_active_sessions_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_active_sessions
    ADD CONSTRAINT radius_active_sessions_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: radius_attributes radius_attributes_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_attributes
    ADD CONSTRAINT radius_attributes_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.radius_profiles(id);


--
-- Name: radius_auth_errors radius_auth_errors_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_auth_errors
    ADD CONSTRAINT radius_auth_errors_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: radius_auth_errors radius_auth_errors_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_auth_errors
    ADD CONSTRAINT radius_auth_errors_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: radius_auth_errors radius_auth_errors_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_auth_errors
    ADD CONSTRAINT radius_auth_errors_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: radius_clients radius_clients_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_clients
    ADD CONSTRAINT radius_clients_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: radius_clients radius_clients_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_clients
    ADD CONSTRAINT radius_clients_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.radius_servers(id);


--
-- Name: radius_sync_jobs radius_sync_jobs_connector_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_sync_jobs
    ADD CONSTRAINT radius_sync_jobs_connector_config_id_fkey FOREIGN KEY (connector_config_id) REFERENCES public.connector_configs(id);


--
-- Name: radius_sync_jobs radius_sync_jobs_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_sync_jobs
    ADD CONSTRAINT radius_sync_jobs_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.radius_servers(id);


--
-- Name: radius_sync_runs radius_sync_runs_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_sync_runs
    ADD CONSTRAINT radius_sync_runs_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.radius_sync_jobs(id);


--
-- Name: radius_users radius_users_access_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_users
    ADD CONSTRAINT radius_users_access_credential_id_fkey FOREIGN KEY (access_credential_id) REFERENCES public.access_credentials(id);


--
-- Name: radius_users radius_users_radius_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_users
    ADD CONSTRAINT radius_users_radius_profile_id_fkey FOREIGN KEY (radius_profile_id) REFERENCES public.radius_profiles(id);


--
-- Name: radius_users radius_users_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_users
    ADD CONSTRAINT radius_users_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: radius_users radius_users_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.radius_users
    ADD CONSTRAINT radius_users_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: role_permissions role_permissions_permission_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES public.permissions(id);


--
-- Name: role_permissions role_permissions_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id);


--
-- Name: router_config_push_results router_config_push_results_post_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_push_results
    ADD CONSTRAINT router_config_push_results_post_snapshot_id_fkey FOREIGN KEY (post_snapshot_id) REFERENCES public.router_config_snapshots(id);


--
-- Name: router_config_push_results router_config_push_results_pre_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_push_results
    ADD CONSTRAINT router_config_push_results_pre_snapshot_id_fkey FOREIGN KEY (pre_snapshot_id) REFERENCES public.router_config_snapshots(id);


--
-- Name: router_config_push_results router_config_push_results_push_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_push_results
    ADD CONSTRAINT router_config_push_results_push_id_fkey FOREIGN KEY (push_id) REFERENCES public.router_config_pushes(id) ON DELETE CASCADE;


--
-- Name: router_config_push_results router_config_push_results_router_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_push_results
    ADD CONSTRAINT router_config_push_results_router_id_fkey FOREIGN KEY (router_id) REFERENCES public.routers(id);


--
-- Name: router_config_pushes router_config_pushes_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_pushes
    ADD CONSTRAINT router_config_pushes_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.router_config_templates(id);


--
-- Name: router_config_snapshots router_config_snapshots_router_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_config_snapshots
    ADD CONSTRAINT router_config_snapshots_router_id_fkey FOREIGN KEY (router_id) REFERENCES public.routers(id) ON DELETE CASCADE;


--
-- Name: router_interfaces router_interfaces_router_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_interfaces
    ADD CONSTRAINT router_interfaces_router_id_fkey FOREIGN KEY (router_id) REFERENCES public.routers(id) ON DELETE CASCADE;


--
-- Name: routers routers_jump_host_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routers
    ADD CONSTRAINT routers_jump_host_id_fkey FOREIGN KEY (jump_host_id) REFERENCES public.jump_hosts(id);


--
-- Name: routers routers_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routers
    ADD CONSTRAINT routers_nas_device_id_fkey FOREIGN KEY (nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: routers routers_network_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routers
    ADD CONSTRAINT routers_network_device_id_fkey FOREIGN KEY (network_device_id) REFERENCES public.network_devices(id);


--
-- Name: service_orders service_orders_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_orders
    ADD CONSTRAINT service_orders_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: service_orders service_orders_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_orders
    ADD CONSTRAINT service_orders_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: service_qualifications service_qualifications_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_qualifications
    ADD CONSTRAINT service_qualifications_address_id_fkey FOREIGN KEY (address_id) REFERENCES public.addresses(id);


--
-- Name: service_qualifications service_qualifications_coverage_area_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_qualifications
    ADD CONSTRAINT service_qualifications_coverage_area_id_fkey FOREIGN KEY (coverage_area_id) REFERENCES public.coverage_areas(id);


--
-- Name: service_state_transitions service_state_transitions_service_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_state_transitions
    ADD CONSTRAINT service_state_transitions_service_order_id_fkey FOREIGN KEY (service_order_id) REFERENCES public.service_orders(id);


--
-- Name: sessions sessions_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: sessions sessions_system_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_system_user_id_fkey FOREIGN KEY (system_user_id) REFERENCES public.system_users(id);


--
-- Name: snmp_pollers snmp_pollers_oid_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_pollers
    ADD CONSTRAINT snmp_pollers_oid_id_fkey FOREIGN KEY (oid_id) REFERENCES public.snmp_oids(id);


--
-- Name: snmp_pollers snmp_pollers_target_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_pollers
    ADD CONSTRAINT snmp_pollers_target_id_fkey FOREIGN KEY (target_id) REFERENCES public.snmp_targets(id);


--
-- Name: snmp_readings snmp_readings_poller_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_readings
    ADD CONSTRAINT snmp_readings_poller_id_fkey FOREIGN KEY (poller_id) REFERENCES public.snmp_pollers(id);


--
-- Name: snmp_targets snmp_targets_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_targets
    ADD CONSTRAINT snmp_targets_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.snmp_credentials(id);


--
-- Name: snmp_targets snmp_targets_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snmp_targets
    ADD CONSTRAINT snmp_targets_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.network_devices(id);


--
-- Name: speed_test_results speed_test_results_network_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.speed_test_results
    ADD CONSTRAINT speed_test_results_network_device_id_fkey FOREIGN KEY (network_device_id) REFERENCES public.network_devices(id);


--
-- Name: speed_test_results speed_test_results_pop_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.speed_test_results
    ADD CONSTRAINT speed_test_results_pop_site_id_fkey FOREIGN KEY (pop_site_id) REFERENCES public.pop_sites(id);


--
-- Name: speed_test_results speed_test_results_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.speed_test_results
    ADD CONSTRAINT speed_test_results_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: speed_test_results speed_test_results_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.speed_test_results
    ADD CONSTRAINT speed_test_results_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: splitter_port_assignments splitter_port_assignments_service_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_port_assignments
    ADD CONSTRAINT splitter_port_assignments_service_address_id_fkey FOREIGN KEY (service_address_id) REFERENCES public.addresses(id);


--
-- Name: splitter_port_assignments splitter_port_assignments_splitter_port_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_port_assignments
    ADD CONSTRAINT splitter_port_assignments_splitter_port_id_fkey FOREIGN KEY (splitter_port_id) REFERENCES public.splitter_ports(id);


--
-- Name: splitter_port_assignments splitter_port_assignments_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_port_assignments
    ADD CONSTRAINT splitter_port_assignments_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: splitter_port_assignments splitter_port_assignments_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_port_assignments
    ADD CONSTRAINT splitter_port_assignments_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: splitter_ports splitter_ports_splitter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitter_ports
    ADD CONSTRAINT splitter_ports_splitter_id_fkey FOREIGN KEY (splitter_id) REFERENCES public.splitters(id);


--
-- Name: splitters splitters_fdh_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitters
    ADD CONSTRAINT splitters_fdh_id_fkey FOREIGN KEY (fdh_id) REFERENCES public.fdh_cabinets(id);


--
-- Name: splitters splitters_zone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splitters
    ADD CONSTRAINT splitters_zone_id_fkey FOREIGN KEY (zone_id) REFERENCES public.network_zones(id);


--
-- Name: splynx_archived_quote_items splynx_archived_quote_items_quote_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_quote_items
    ADD CONSTRAINT splynx_archived_quote_items_quote_id_fkey FOREIGN KEY (quote_id) REFERENCES public.splynx_archived_quotes(id);


--
-- Name: splynx_archived_quotes splynx_archived_quotes_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_quotes
    ADD CONSTRAINT splynx_archived_quotes_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: splynx_archived_ticket_messages splynx_archived_ticket_messages_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_ticket_messages
    ADD CONSTRAINT splynx_archived_ticket_messages_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.splynx_archived_tickets(id);


--
-- Name: splynx_archived_tickets splynx_archived_tickets_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.splynx_archived_tickets
    ADD CONSTRAINT splynx_archived_tickets_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: stored_files stored_files_owner_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stored_files
    ADD CONSTRAINT stored_files_owner_subscriber_id_fkey FOREIGN KEY (owner_subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: stored_files stored_files_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stored_files
    ADD CONSTRAINT stored_files_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.subscribers(id);


--
-- Name: subscriber_channels subscriber_channels_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_channels
    ADD CONSTRAINT subscriber_channels_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: subscriber_custom_fields subscriber_custom_fields_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_custom_fields
    ADD CONSTRAINT subscriber_custom_fields_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: subscriber_permissions subscriber_permissions_granted_by_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_permissions
    ADD CONSTRAINT subscriber_permissions_granted_by_subscriber_id_fkey FOREIGN KEY (granted_by_subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: subscriber_permissions subscriber_permissions_permission_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_permissions
    ADD CONSTRAINT subscriber_permissions_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES public.permissions(id);


--
-- Name: subscriber_permissions subscriber_permissions_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_permissions
    ADD CONSTRAINT subscriber_permissions_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: subscriber_roles subscriber_roles_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_roles
    ADD CONSTRAINT subscriber_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id);


--
-- Name: subscriber_roles subscriber_roles_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriber_roles
    ADD CONSTRAINT subscriber_roles_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: subscribers subscribers_pop_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscribers
    ADD CONSTRAINT subscribers_pop_site_id_fkey FOREIGN KEY (pop_site_id) REFERENCES public.pop_sites(id) ON DELETE SET NULL;


--
-- Name: subscribers subscribers_reseller_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscribers
    ADD CONSTRAINT subscribers_reseller_id_fkey FOREIGN KEY (reseller_id) REFERENCES public.resellers(id);


--
-- Name: subscribers subscribers_tax_rate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscribers
    ADD CONSTRAINT subscribers_tax_rate_id_fkey FOREIGN KEY (tax_rate_id) REFERENCES public.tax_rates(id);


--
-- Name: subscription_add_ons subscription_add_ons_add_on_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_add_ons
    ADD CONSTRAINT subscription_add_ons_add_on_id_fkey FOREIGN KEY (add_on_id) REFERENCES public.add_ons(id);


--
-- Name: subscription_add_ons subscription_add_ons_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_add_ons
    ADD CONSTRAINT subscription_add_ons_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: subscription_change_requests subscription_change_requests_current_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_change_requests
    ADD CONSTRAINT subscription_change_requests_current_offer_id_fkey FOREIGN KEY (current_offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: subscription_change_requests subscription_change_requests_requested_by_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_change_requests
    ADD CONSTRAINT subscription_change_requests_requested_by_subscriber_id_fkey FOREIGN KEY (requested_by_subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: subscription_change_requests subscription_change_requests_requested_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_change_requests
    ADD CONSTRAINT subscription_change_requests_requested_offer_id_fkey FOREIGN KEY (requested_offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: subscription_change_requests subscription_change_requests_reviewed_by_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_change_requests
    ADD CONSTRAINT subscription_change_requests_reviewed_by_subscriber_id_fkey FOREIGN KEY (reviewed_by_subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: subscription_change_requests subscription_change_requests_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_change_requests
    ADD CONSTRAINT subscription_change_requests_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: subscription_engine_settings subscription_engine_settings_engine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_engine_settings
    ADD CONSTRAINT subscription_engine_settings_engine_id_fkey FOREIGN KEY (engine_id) REFERENCES public.subscription_engines(id);


--
-- Name: subscription_lifecycle_events subscription_lifecycle_events_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_lifecycle_events
    ADD CONSTRAINT subscription_lifecycle_events_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: subscriptions subscriptions_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.catalog_offers(id);


--
-- Name: subscriptions subscriptions_offer_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_offer_version_id_fkey FOREIGN KEY (offer_version_id) REFERENCES public.offer_versions(id);


--
-- Name: subscriptions subscriptions_provisioning_nas_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_provisioning_nas_device_id_fkey FOREIGN KEY (provisioning_nas_device_id) REFERENCES public.nas_devices(id);


--
-- Name: subscriptions subscriptions_radius_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_radius_profile_id_fkey FOREIGN KEY (radius_profile_id) REFERENCES public.radius_profiles(id);


--
-- Name: subscriptions subscriptions_service_address_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_service_address_id_fkey FOREIGN KEY (service_address_id) REFERENCES public.addresses(id);


--
-- Name: subscriptions subscriptions_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: support_ticket_assignees support_ticket_assignees_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_assignees
    ADD CONSTRAINT support_ticket_assignees_person_id_fkey FOREIGN KEY (person_id) REFERENCES public.subscribers(id);


--
-- Name: support_ticket_assignees support_ticket_assignees_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_assignees
    ADD CONSTRAINT support_ticket_assignees_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.support_tickets(id);


--
-- Name: support_ticket_comments support_ticket_comments_author_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_comments
    ADD CONSTRAINT support_ticket_comments_author_person_id_fkey FOREIGN KEY (author_person_id) REFERENCES public.subscribers(id);


--
-- Name: support_ticket_comments support_ticket_comments_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_comments
    ADD CONSTRAINT support_ticket_comments_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.support_tickets(id);


--
-- Name: support_ticket_links support_ticket_links_created_by_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_links
    ADD CONSTRAINT support_ticket_links_created_by_person_id_fkey FOREIGN KEY (created_by_person_id) REFERENCES public.subscribers(id);


--
-- Name: support_ticket_links support_ticket_links_from_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_links
    ADD CONSTRAINT support_ticket_links_from_ticket_id_fkey FOREIGN KEY (from_ticket_id) REFERENCES public.support_tickets(id);


--
-- Name: support_ticket_links support_ticket_links_to_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_links
    ADD CONSTRAINT support_ticket_links_to_ticket_id_fkey FOREIGN KEY (to_ticket_id) REFERENCES public.support_tickets(id);


--
-- Name: support_ticket_merges support_ticket_merges_merged_by_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_merges
    ADD CONSTRAINT support_ticket_merges_merged_by_person_id_fkey FOREIGN KEY (merged_by_person_id) REFERENCES public.subscribers(id);


--
-- Name: support_ticket_merges support_ticket_merges_source_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_merges
    ADD CONSTRAINT support_ticket_merges_source_ticket_id_fkey FOREIGN KEY (source_ticket_id) REFERENCES public.support_tickets(id);


--
-- Name: support_ticket_merges support_ticket_merges_target_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_merges
    ADD CONSTRAINT support_ticket_merges_target_ticket_id_fkey FOREIGN KEY (target_ticket_id) REFERENCES public.support_tickets(id);


--
-- Name: support_ticket_sla_events support_ticket_sla_events_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_sla_events
    ADD CONSTRAINT support_ticket_sla_events_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.support_tickets(id);


--
-- Name: support_tickets support_tickets_assigned_to_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_assigned_to_person_id_fkey FOREIGN KEY (assigned_to_person_id) REFERENCES public.subscribers(id);


--
-- Name: support_tickets support_tickets_created_by_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_created_by_person_id_fkey FOREIGN KEY (created_by_person_id) REFERENCES public.subscribers(id);


--
-- Name: support_tickets support_tickets_customer_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_customer_account_id_fkey FOREIGN KEY (customer_account_id) REFERENCES public.subscribers(id);


--
-- Name: support_tickets support_tickets_customer_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_customer_person_id_fkey FOREIGN KEY (customer_person_id) REFERENCES public.subscribers(id);


--
-- Name: support_tickets support_tickets_merged_into_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_merged_into_ticket_id_fkey FOREIGN KEY (merged_into_ticket_id) REFERENCES public.support_tickets(id);


--
-- Name: support_tickets support_tickets_site_coordinator_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_site_coordinator_person_id_fkey FOREIGN KEY (site_coordinator_person_id) REFERENCES public.subscribers(id);


--
-- Name: support_tickets support_tickets_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: support_tickets support_tickets_technician_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_technician_person_id_fkey FOREIGN KEY (technician_person_id) REFERENCES public.subscribers(id);


--
-- Name: support_tickets support_tickets_ticket_manager_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_ticket_manager_person_id_fkey FOREIGN KEY (ticket_manager_person_id) REFERENCES public.subscribers(id);


--
-- Name: system_user_permissions system_user_permissions_granted_by_system_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_permissions
    ADD CONSTRAINT system_user_permissions_granted_by_system_user_id_fkey FOREIGN KEY (granted_by_system_user_id) REFERENCES public.system_users(id);


--
-- Name: system_user_permissions system_user_permissions_permission_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_permissions
    ADD CONSTRAINT system_user_permissions_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES public.permissions(id);


--
-- Name: system_user_permissions system_user_permissions_system_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_permissions
    ADD CONSTRAINT system_user_permissions_system_user_id_fkey FOREIGN KEY (system_user_id) REFERENCES public.system_users(id);


--
-- Name: system_user_roles system_user_roles_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_roles
    ADD CONSTRAINT system_user_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id);


--
-- Name: system_user_roles system_user_roles_system_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_user_roles
    ADD CONSTRAINT system_user_roles_system_user_id_fkey FOREIGN KEY (system_user_id) REFERENCES public.system_users(id);


--
-- Name: table_column_config table_column_config_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_column_config
    ADD CONSTRAINT table_column_config_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.subscribers(id);


--
-- Name: tr069_cpe_devices tr069_cpe_devices_acs_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_cpe_devices
    ADD CONSTRAINT tr069_cpe_devices_acs_server_id_fkey FOREIGN KEY (acs_server_id) REFERENCES public.tr069_acs_servers(id);


--
-- Name: tr069_cpe_devices tr069_cpe_devices_cpe_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_cpe_devices
    ADD CONSTRAINT tr069_cpe_devices_cpe_device_id_fkey FOREIGN KEY (cpe_device_id) REFERENCES public.cpe_devices(id);


--
-- Name: tr069_cpe_devices tr069_cpe_devices_ont_unit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_cpe_devices
    ADD CONSTRAINT tr069_cpe_devices_ont_unit_id_fkey FOREIGN KEY (ont_unit_id) REFERENCES public.ont_units(id);


--
-- Name: tr069_jobs tr069_jobs_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_jobs
    ADD CONSTRAINT tr069_jobs_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.tr069_cpe_devices(id);


--
-- Name: tr069_parameter_maps tr069_parameter_maps_capability_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_parameter_maps
    ADD CONSTRAINT tr069_parameter_maps_capability_id_fkey FOREIGN KEY (capability_id) REFERENCES public.vendor_model_capabilities(id) ON DELETE CASCADE;


--
-- Name: tr069_parameters tr069_parameters_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_parameters
    ADD CONSTRAINT tr069_parameters_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.tr069_cpe_devices(id);


--
-- Name: tr069_sessions tr069_sessions_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tr069_sessions
    ADD CONSTRAINT tr069_sessions_device_id_fkey FOREIGN KEY (device_id) REFERENCES public.tr069_cpe_devices(id);


--
-- Name: usage_charges usage_charges_invoice_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_charges
    ADD CONSTRAINT usage_charges_invoice_line_id_fkey FOREIGN KEY (invoice_line_id) REFERENCES public.invoice_lines(id);


--
-- Name: usage_charges usage_charges_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_charges
    ADD CONSTRAINT usage_charges_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: usage_charges usage_charges_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_charges
    ADD CONSTRAINT usage_charges_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: usage_records usage_records_quota_bucket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_records
    ADD CONSTRAINT usage_records_quota_bucket_id_fkey FOREIGN KEY (quota_bucket_id) REFERENCES public.quota_buckets(id);


--
-- Name: usage_records usage_records_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_records
    ADD CONSTRAINT usage_records_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: user_credentials user_credentials_radius_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_credentials
    ADD CONSTRAINT user_credentials_radius_server_id_fkey FOREIGN KEY (radius_server_id) REFERENCES public.radius_servers(id);


--
-- Name: user_credentials user_credentials_subscriber_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_credentials
    ADD CONSTRAINT user_credentials_subscriber_id_fkey FOREIGN KEY (subscriber_id) REFERENCES public.subscribers(id);


--
-- Name: user_credentials user_credentials_system_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_credentials
    ADD CONSTRAINT user_credentials_system_user_id_fkey FOREIGN KEY (system_user_id) REFERENCES public.system_users(id);


--
-- Name: vlans vlans_olt_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vlans
    ADD CONSTRAINT vlans_olt_device_id_fkey FOREIGN KEY (olt_device_id) REFERENCES public.olt_devices(id) ON DELETE SET NULL;


--
-- Name: vlans vlans_region_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vlans
    ADD CONSTRAINT vlans_region_id_fkey FOREIGN KEY (region_id) REFERENCES public.region_zones(id);


--
-- Name: webhook_deliveries webhook_deliveries_endpoint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_endpoint_id_fkey FOREIGN KEY (endpoint_id) REFERENCES public.webhook_endpoints(id);


--
-- Name: webhook_deliveries webhook_deliveries_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.webhook_subscriptions(id);


--
-- Name: webhook_endpoints webhook_endpoints_connector_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_endpoints
    ADD CONSTRAINT webhook_endpoints_connector_config_id_fkey FOREIGN KEY (connector_config_id) REFERENCES public.connector_configs(id);


--
-- Name: webhook_subscriptions webhook_subscriptions_endpoint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_subscriptions
    ADD CONSTRAINT webhook_subscriptions_endpoint_id_fkey FOREIGN KEY (endpoint_id) REFERENCES public.webhook_endpoints(id);


--
-- Name: wireguard_connection_logs wireguard_connection_logs_peer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wireguard_connection_logs
    ADD CONSTRAINT wireguard_connection_logs_peer_id_fkey FOREIGN KEY (peer_id) REFERENCES public.wireguard_peers(id);


--
-- Name: wireguard_peers wireguard_peers_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wireguard_peers
    ADD CONSTRAINT wireguard_peers_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.wireguard_servers(id);


--
-- PostgreSQL database dump complete
--
