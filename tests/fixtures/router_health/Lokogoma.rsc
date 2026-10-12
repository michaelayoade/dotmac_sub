# <date> by RouterOS 7.13.2
/snmp community
set [ find default=yes ] addresses=192.0.2.1/32,192.0.2.2/32,192.0.2.3/32,192.0.2.4/32
/interface pppoe-server server
add default-profile=default-encryption disabled=no interface="sfp-sfpplus12=>To Switch" one-session-per-host=yes service-name=fastnet
/ip firewall address-list
add address=192.0.2.5 list=suspended
add address=192.0.2.6 list=suspended
add address=192.0.2.7 list=suspended
add address=192.0.2.8 list=suspended
add address=192.0.2.9 list=suspended
add address=192.0.2.10 list=suspended
add address=192.0.2.11 list=suspended
add address=192.0.2.12 list=suspended
add address=192.0.2.13 list=suspended
add address=192.0.2.14 list=suspended
add address=192.0.2.15 list=suspended
add address=192.0.2.16 list=suspended
add address=192.0.2.17 list=suspended
add address=192.0.2.18 list=suspended
add address=192.0.2.19 list=suspended
add address=192.0.2.20 list=suspended
add address=192.0.2.21 list=suspended
add address=192.0.2.22 list=suspended
add address=192.0.2.23 list=suspended
add address=192.0.2.24 list=suspended
add address=192.0.2.25 list=suspended
add address=192.0.2.26 list=suspended
add address=192.0.2.27 list=suspended
add address=192.0.2.28 list=suspended
add address=192.0.2.29 list=suspended
add address=192.0.2.30 list=suspended
add address=192.0.2.31 list=suspended
add address=192.0.2.32 list=suspended
add address=192.0.2.33 list=suspended
add address=192.0.2.34 list=suspended
add address=192.0.2.35 list=suspended
add address=192.0.2.36 list=suspended
add address=192.0.2.37 list=suspended
add address=192.0.2.38 list=suspended
add address=192.0.2.39 list=suspended
add address=192.0.2.40 list=suspended
add address=192.0.2.41 list=suspended
add address=192.0.2.42 list=suspended
add address=192.0.2.43 list=suspended
add address=192.0.2.44 list=suspended
add address=192.0.2.45 list=suspended
add address=192.0.2.46 list=suspended
add address=192.0.2.47 list=suspended
add address=192.0.2.48 list=suspended
add address=192.0.2.49 list=suspended
add address=192.0.2.50 list=suspended
add address=192.0.2.51 list=suspended
add address=192.0.2.52 list=suspended
add address=192.0.2.53 list=suspended
add address=192.0.2.54 list=suspended
add address=192.0.2.55 list=suspended
add address=192.0.2.56 list=suspended
add address=192.0.2.57 list=suspended
add address=192.0.2.58 list=suspended
add address=192.0.2.59 list=suspended
add address=192.0.2.60 list=suspended
add address=192.0.2.61 list=suspended
add address=192.0.2.62 list=suspended
add address=192.0.2.63 list=suspended
add address=192.0.2.64 list=suspended
add address=192.0.2.65 list=suspended
add address=192.0.2.66 list=suspended
add address=192.0.2.67 list=suspended
add address=192.0.2.68 list=suspended
add address=192.0.2.69 list=suspended
add address=192.0.2.70 list=suspended
add address=192.0.2.71 list=suspended
add address=192.0.2.72 list=suspended
add address=192.0.2.73 list=suspended
add address=192.0.2.74 list=suspended
add address=192.0.2.75 list=suspended
add address=192.0.2.76 list=suspended
add address=192.0.2.77 list=suspended
add address=192.0.2.78 list=suspended
add address=192.0.2.79 list=suspended
add address=192.0.2.80 list=suspended
add address=192.0.2.81 list=suspended
add address=192.0.2.82 list=suspended
add address=192.0.2.83 list=suspended
add address=192.0.2.84 list=suspended
add address=192.0.2.3 comment="INPUT-PROTECT: sub-prod (API 8728, SSH dotmac-ops)" list=ipp-subprod
/ip firewall filter
add action=jump chain=forward comment="dotmac suspended quarantine" dst-address-list=!splynx-allowed-resources jump-target=dotmac-suspended src-address-list=suspended
add action=accept chain=splynx-blocked comment="dotmac portal allow (suspended)" dst-address=192.0.2.3 dst-port=80,443 protocol=tcp
add action=accept chain=input comment="INPUT-PROTECT: accept established/related" connection-state=established,related
add action=drop chain=input comment="INPUT-PROTECT: drop invalid" connection-state=invalid
add action=accept chain=input comment="INPUT-PROTECT: accept ICMP" protocol=icmp
add action=accept chain=input comment="INPUT-PROTECT: accept OSPF" protocol=ospf
add action=accept chain=input comment="INPUT-PROTECT: accept BGP from iBGP peers" dst-port=179 protocol=tcp src-address-list=ipp-bgp
add action=accept chain=input comment="INPUT-PROTECT: accept SSH from mgmt" dst-port=120 protocol=tcp src-address-list=ipp-mgmt
add action=accept chain=input comment="INPUT-PROTECT: accept SSH from sub-prod" dst-port=120 protocol=tcp src-address-list=ipp-subprod
add action=accept chain=input comment="INPUT-PROTECT: accept Winbox from mgmt" dst-port=8291 protocol=tcp src-address-list=ipp-mgmt
add action=accept chain=input comment="INPUT-PROTECT: accept REST www-ssl" dst-port=443 protocol=tcp src-address-list=ipp-rest
add action=accept chain=input comment="INPUT-PROTECT: accept API from sub-prod" dst-port=8728 protocol=tcp src-address-list=ipp-subprod
add action=accept chain=input comment="INPUT-PROTECT: accept RADIUS CoA/disconnect" dst-port=3799 protocol=udp src-address-list=ipp-radius
add action=accept chain=input comment="INPUT-PROTECT: accept SNMP from pollers" dst-port=161 protocol=udp src-address-list=ipp-snmp
add action=accept chain=input comment="INPUT-PROTECT: accept NTP from internal" dst-port=123 protocol=udp src-address-list=ipp-internal
add action=accept chain=input comment="INPUT-PROTECT: accept btest TCP from mgmt" dst-port=2000 protocol=tcp src-address-list=ipp-mgmt
add action=accept chain=input comment="INPUT-PROTECT: accept btest UDP from mgmt" dst-port=2000-2100 protocol=udp src-address-list=ipp-mgmt
add action=log chain=input comment="INPUT-PROTECT: log would-drop from internal" limit=20/1m,20:packet log-prefix=IPP-WD-INT src-address-list=ipp-internal
add action=accept chain=dotmac-suspended comment="dotmac suspended dns allow" dst-port=53 protocol=udp
add action=accept chain=dotmac-suspended comment="dotmac suspended portal allow" dst-address=192.0.2.3 dst-port=80,443 protocol=tcp
add action=reject chain=dotmac-suspended comment="dotmac suspended reject" reject-with=icmp-admin-prohibited
add action=drop chain=dotmac-suspended comment="dotmac suspended drop"
add action=drop chain=input comment="INPUT-PROTECT: drop all else"
/ip firewall nat
/ip service
set telnet disabled=yes
set ftp disabled=yes
set www address=192.0.2.85/22,192.0.2.86/22,192.0.2.87/24,192.0.2.88/12 disabled=yes port=1256
set ssh address="192.0.2.85/22,192.0.2.86/22,192.0.2.87/24,192.0.2.88/12,192.0.2.3/32" port=120
set www-ssl address=192.0.2.3/32,192.0.2.89/32 certificate=REDACTED disabled=no
set api address=192.0.2.3/32
set winbox address=192.0.2.85/22,192.0.2.86/22,192.0.2.87/24,192.0.2.88/12
set api-ssl disabled=yes
/ipv6 firewall filter
add action=accept chain=input comment="INPUT6-PROTECT: accept established/related" connection-state=established,related
add action=accept chain=input comment="INPUT6-PROTECT: accept ICMPv6" protocol=icmpv6
add action=accept chain=input comment="INPUT6-PROTECT: accept BGP from configured v6 peers" port=179 protocol=tcp src-address-list=ipp6-bgp
add action=accept chain=input comment="INPUT6-PROTECT: accept OSPFv3 from link-local" protocol=ospf src-address=2001:db8::1/10
add action=accept chain=input comment="INPUT6-PROTECT: accept DHCPv6 server from link-local" dst-port=547 protocol=udp src-address=2001:db8::1/10
add action=log chain=input comment="INPUT6-PROTECT: log would-drop internal" limit=20/1m,20:packet log-prefix=IPP6-WD-INT src-address-list=ipp6-internal
add action=log chain=input comment="INPUT6-PROTECT: log would-drop external" limit=10/1m,10:packet log-prefix=IPP6-WD-EXT src-address-list=!ipp6-internal
add action=passthrough chain=input comment="INPUT6-PROTECT: count would-drop"
/radius
add address=192.0.2.3 comment="dotmac_sub mirror" service=ppp,hotspot,wireless src-address=192.0.2.90 timeout=3s
add accounting-port=1823 address=192.0.2.3 authentication-port=1822 comment="dotmac device-login" service=login src-address=192.0.2.90 timeout=3s
/radius incoming
set accept=yes
/routing bgp connection
add as=328160 connect=yes disabled=no listen=yes local.address=192.0.2.90 .role=ibgp name="Abuja GBB" output.no-client-to-client-reflection=yes remote.address=192.0.2.91/32 .as=328160 .port=179 router-id=192.0.2.90 routing-table=main templates=default
add as=328160 connect=yes disabled=no input.filter=garki-core-in listen=yes local.address=192.0.2.90 .role=ibgp name="Abuja Core Garki" output.no-client-to-client-reflection=yes remote.address=192.0.2.92/32 .as=328160 router-id=192.0.2.90 routing-table=main templates=default
add address-families=ipv6 as=328160 connect=yes listen=yes local.address=2001:db8::2 .role=ibgp name="Gudu V6" remote.address=2001:db8::3/128 .as=328160 router-id=192.0.2.90 routing-table=main
add as=328160 connect=yes input.filter=medallion-in listen=yes local.address=192.0.2.90 .role=ibgp name=Abl-Medallion output.network=bgp-networks .no-client-to-client-reflection=yes remote.address=192.0.2.93/32 .as=328160 router-id=192.0.2.90 routing-table=main templates=default
/snmp
set contact=user@example.invalid enabled=yes location=abuja src-address=192.0.2.90 trap-generators=interfaces,start-trap trap-target=192.0.2.1 trap-version=2
/system identity
set name="Lokogoma Access"
/system ntp client
set enabled=yes
/system ntp client servers
add address=192.0.2.92
