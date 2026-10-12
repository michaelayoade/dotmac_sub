# <date> by RouterOS 7.16.2
/snmp community
set [ find default=yes ] addresses=192.0.2.1/32,192.0.2.2/32,192.0.2.3/32,192.0.2.4/32
/interface pppoe-server server
add default-profile=default-encryption disabled=no interface="Internet Access Bridge" one-session-per-host=yes service-name=isp-service-a
/ip firewall address-list
add address=192.0.2.94 disabled=yes list=suspended
add address=192.0.2.95 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=192.0.2.96 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=192.0.2.97 comment="dotmac migrated after app/radius reconcile" disabled=yes list=suspended
add address=192.0.2.98 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=192.0.2.99 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=192.0.2.100 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=192.0.2.101 list=suspended
add address=192.0.2.102 list=suspended
add address=192.0.2.103 list=suspended
add address=192.0.2.104 list=suspended
add address=192.0.2.105 list=suspended
add address=192.0.2.106 list=suspended
add address=192.0.2.107 list=suspended
add address=192.0.2.108 list=suspended
add address=192.0.2.109 list=suspended
add address=192.0.2.110 list=suspended
add address=192.0.2.111 list=suspended
add address=192.0.2.112 list=suspended
add address=192.0.2.113 list=suspended
add address=192.0.2.114 list=suspended
add address=192.0.2.115 list=suspended
add address=192.0.2.116 list=suspended
add address=192.0.2.117 list=suspended
add address=192.0.2.118 list=suspended
add address=192.0.2.119 list=suspended
add address=192.0.2.120 list=suspended
add address=192.0.2.121 list=suspended
add address=192.0.2.122 list=suspended
add address=192.0.2.123 list=suspended
add address=192.0.2.124 list=suspended
add address=192.0.2.125 list=suspended
add address=192.0.2.126 list=suspended
add address=192.0.2.127 list=suspended
add address=192.0.2.128 list=suspended
add address=192.0.2.129 list=suspended
add address=192.0.2.130 list=suspended
add address=192.0.2.131 list=suspended
add address=192.0.2.132 list=suspended
add address=192.0.2.133 list=suspended
add address=192.0.2.134 list=suspended
add address=192.0.2.135 list=suspended
add address=192.0.2.136 list=suspended
add address=192.0.2.137 list=suspended
add address=192.0.2.138 list=suspended
add address=192.0.2.139 list=suspended
add address=192.0.2.140 list=suspended
add address=192.0.2.141 list=suspended
add address=192.0.2.142 list=suspended
add address=192.0.2.143 list=suspended
add address=192.0.2.144 list=suspended
add address=192.0.2.145 list=suspended
add address=192.0.2.146 list=suspended
add address=192.0.2.147 list=suspended
add address=192.0.2.148 list=suspended
add address=192.0.2.149 list=suspended
add address=192.0.2.150 list=suspended
add address=192.0.2.151 list=suspended
add address=192.0.2.152 list=suspended
add address=192.0.2.153 list=suspended
add address=192.0.2.154 list=suspended
add address=192.0.2.155 list=suspended
add address=192.0.2.156 list=suspended
add address=192.0.2.157 list=suspended
add address=192.0.2.158 list=suspended
add address=192.0.2.159 list=suspended
add address=192.0.2.160 list=suspended
add address=192.0.2.161 list=suspended
add address=192.0.2.162 list=suspended
add address=192.0.2.163 list=suspended
add address=192.0.2.164 list=suspended
add address=192.0.2.165 list=suspended
add address=192.0.2.166 list=suspended
add address=192.0.2.167 list=suspended
add address=192.0.2.168 list=suspended
add address=192.0.2.169 list=suspended
add address=192.0.2.170 list=suspended
add address=192.0.2.171 list=suspended
add address=192.0.2.172 list=suspended
add address=192.0.2.173 list=suspended
add address=192.0.2.174 list=suspended
add address=192.0.2.175 list=suspended
add address=192.0.2.176 list=suspended
add address=192.0.2.177 list=suspended
add address=192.0.2.178 list=suspended
add address=192.0.2.179 list=suspended
add address=192.0.2.180 list=suspended
add address=192.0.2.181 list=suspended
add address=192.0.2.182 list=suspended
add address=192.0.2.183 list=suspended
add address=192.0.2.184 list=suspended
add address=192.0.2.185 list=suspended
add address=192.0.2.186 list=suspended
add address=192.0.2.187 list=suspended
add address=192.0.2.188 list=suspended
add address=192.0.2.189 list=suspended
add address=192.0.2.190 list=suspended
add address=192.0.2.191 list=suspended
add address=192.0.2.192 list=suspended
add address=192.0.2.193 list=suspended
add address=192.0.2.194 list=suspended
add address=192.0.2.195 list=suspended
add address=192.0.2.196 list=suspended
add address=192.0.2.197 list=suspended
add address=192.0.2.198 list=suspended
add address=192.0.2.199 list=suspended
add address=192.0.2.200 list=suspended
add address=192.0.2.201 list=suspended
add address=192.0.2.202 list=suspended
add address=192.0.2.203 list=suspended
add address=192.0.2.204 list=suspended
add address=192.0.2.205 list=suspended
add address=192.0.2.206 list=suspended
add address=192.0.2.207 list=suspended
add address=192.0.2.208 list=suspended
add address=192.0.2.209 list=suspended
add address=192.0.2.210 list=suspended
add address=192.0.2.211 list=suspended
add address=192.0.2.212 list=suspended
add address=192.0.2.213 list=suspended
add address=192.0.2.214 list=suspended
add address=192.0.2.215 list=suspended
add address=192.0.2.216 list=suspended
add address=192.0.2.217 list=suspended
add address=192.0.2.218 list=suspended
add address=192.0.2.219 list=suspended
add address=192.0.2.220 list=suspended
add address=192.0.2.221 list=suspended
add address=192.0.2.222 list=suspended
add address=192.0.2.223 list=suspended
add address=192.0.2.224 list=suspended
add address=192.0.2.225 list=suspended
add address=192.0.2.226 list=suspended
add address=192.0.2.227 list=suspended
add address=192.0.2.228 list=suspended
add address=192.0.2.229 list=suspended
add address=192.0.2.230 list=suspended
add address=192.0.2.231 list=suspended
add address=192.0.2.232 list=suspended
add address=192.0.2.233 list=suspended
add address=192.0.2.234 list=suspended
add address=192.0.2.235 list=suspended
add address=192.0.2.236 list=suspended
add address=192.0.2.237 list=suspended
add address=192.0.2.238 list=suspended
add address=192.0.2.239 list=suspended
add address=192.0.2.240 list=suspended
add address=192.0.2.241 list=suspended
add address=192.0.2.242 list=suspended
add address=192.0.2.243 list=suspended
add address=192.0.2.244 list=suspended
add address=192.0.2.245 list=suspended
add address=192.0.2.246 list=suspended
add address=192.0.2.247 list=suspended
add address=192.0.2.248 list=suspended
add address=192.0.2.249 list=suspended
add address=192.0.2.250 list=suspended
add address=192.0.2.251 list=suspended
add address=192.0.2.252 list=suspended
add address=192.0.2.253 list=suspended
add address=192.0.2.254 list=suspended
add address=198.51.100.1 list=suspended
add address=198.51.100.2 list=suspended
add address=198.51.100.3 list=suspended
add address=198.51.100.4 list=suspended
add address=198.51.100.5 list=suspended
add address=198.51.100.6 list=suspended
add address=198.51.100.7 list=suspended
add address=198.51.100.8 list=suspended
add address=198.51.100.9 list=suspended
add address=198.51.100.10 list=suspended
add address=198.51.100.11 list=suspended
add address=198.51.100.12 list=suspended
add address=198.51.100.13 list=suspended
add address=198.51.100.14 list=suspended
add address=198.51.100.15 list=suspended
add address=198.51.100.16 list=suspended
add address=198.51.100.17 list=suspended
add address=198.51.100.18 list=suspended
add address=198.51.100.19 list=suspended
add address=198.51.100.20 list=suspended
add address=198.51.100.21 list=suspended
add address=198.51.100.22 list=suspended
add address=198.51.100.23 list=suspended
add address=198.51.100.24 list=suspended
add address=198.51.100.25 list=suspended
add address=198.51.100.26 list=suspended
add address=198.51.100.27 list=suspended
add address=198.51.100.28 list=suspended
add address=198.51.100.29 list=suspended
add address=198.51.100.30 list=suspended
add address=198.51.100.31 list=suspended
add address=198.51.100.32 list=suspended
add address=198.51.100.33 list=suspended
add address=198.51.100.34 list=suspended
add address=198.51.100.35 list=suspended
add address=198.51.100.36 list=suspended
add address=198.51.100.37 list=suspended
add address=198.51.100.38 list=suspended
add address=198.51.100.39 list=suspended
add address=198.51.100.40 list=suspended
add address=198.51.100.41 list=suspended
add address=198.51.100.42 list=suspended
add address=198.51.100.43 list=suspended
add address=198.51.100.44 list=suspended
add address=198.51.100.45 list=suspended
add address=198.51.100.46 list=suspended
add address=198.51.100.47 list=suspended
add address=198.51.100.48 list=suspended
add address=198.51.100.49 list=suspended
add address=198.51.100.50 list=suspended
add address=198.51.100.51 list=suspended
add address=198.51.100.52 list=suspended
add address=198.51.100.53 list=suspended
add address=198.51.100.54 list=suspended
add address=198.51.100.55 list=suspended
add address=198.51.100.56 list=suspended
add address=198.51.100.57 list=suspended
add address=198.51.100.58 list=suspended
add address=198.51.100.59 list=suspended
add address=198.51.100.60 list=suspended
add address=198.51.100.61 list=suspended
add address=198.51.100.62 list=suspended
add address=198.51.100.63 list=suspended
add address=198.51.100.64 list=suspended
add address=198.51.100.65 list=suspended
add address=198.51.100.66 list=suspended
add address=198.51.100.67 list=suspended
add address=198.51.100.68 list=suspended
add address=198.51.100.69 list=suspended
add address=198.51.100.70 list=suspended
add address=198.51.100.71 list=suspended
add address=198.51.100.72 list=suspended
add address=198.51.100.73 list=suspended
add address=198.51.100.74 list=suspended
add address=198.51.100.75 list=suspended
add address=198.51.100.76 list=suspended
add address=198.51.100.77 list=suspended
add address=198.51.100.78 list=suspended
add address=198.51.100.79 list=suspended
add address=198.51.100.80 list=suspended
add address=198.51.100.81 list=suspended
add address=198.51.100.82 list=suspended
add address=198.51.100.83 list=suspended
add address=198.51.100.84 list=suspended
add address=198.51.100.85 list=suspended
add address=198.51.100.86 list=suspended
add address=198.51.100.87 list=suspended
add address=198.51.100.88 list=suspended
add address=198.51.100.89 list=suspended
add address=198.51.100.90 list=suspended
add address=198.51.100.91 list=suspended
add address=198.51.100.92 list=suspended
add address=198.51.100.93 list=suspended
add address=198.51.100.94 list=suspended
add address=192.0.2.3 comment="INPUT-PROTECT: sub-prod (API 8728, SSH dotmac-ops)" list=ipp-subprod
add address=198.51.100.95 list=suspended
add address=198.51.100.96 list=suspended
/ip firewall filter
add action=jump chain=forward comment="dotmac suspended quarantine" dst-address-list=!splynx-allowed-resources jump-target=dotmac-suspended src-address-list=suspended
add action=accept chain=splynx-blocked comment="dotmac portal allow (suspended)" dst-address=192.0.2.3 dst-port=80,443 protocol=tcp
add action=accept chain=dotmac-suspended comment="dotmac suspended dns allow" dst-port=53 protocol=udp
add action=accept chain=dotmac-suspended comment="dotmac suspended portal allow" dst-address=192.0.2.3 dst-port=80,443 protocol=tcp
add action=reject chain=dotmac-suspended comment="dotmac suspended reject" reject-with=icmp-admin-prohibited
add action=drop chain=dotmac-suspended comment="dotmac suspended drop"
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
add action=accept chain=input comment="INPUT-PROTECT: accept EoIP/GRE from peer site" protocol=gre src-address-list=ipp-eoip
add action=log chain=input comment="INPUT-PROTECT: log would-drop from internal" limit=20/1m,20:packet log-prefix=IPP-WD-INT src-address-list=ipp-internal
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
add address=192.0.2.3 comment="dotmac_sub mirror" service=ppp,hotspot,wireless src-address=198.51.100.97 timeout=3s
add accounting-port=1823 address=192.0.2.3 authentication-port=1822 comment="dotmac device-login" service=login src-address=198.51.100.97 timeout=3s
/radius incoming
set accept=yes
/routing bgp connection
add as=328160 connect=yes disabled=no listen=yes local.address=198.51.100.97 .role=ibgp name="Abuja GBB" output.network=bgp-networks .no-client-to-client-reflection=yes remote.address=192.0.2.91/32 .as=328160 .port=179 router-id=198.51.100.97 routing-table=main templates=default
add as=328160 connect=yes disabled=no input.filter=garki-core-in listen=yes local.address=198.51.100.97 .role=ibgp name="Abuja Core Garki" output.network=bgp-networks .no-client-to-client-reflection=yes remote.address=192.0.2.92/32 .as=328160 router-id=198.51.100.97 routing-table=main templates=default
add address-families=ipv6 as=328160 connect=yes listen=yes local.address=2001:db8::4 .role=ibgp name="Garki Core V6" remote.address=2001:db8::5/128 .as=328160 router-id=198.51.100.97 routing-table=main
add address-families=ipv6 as=328160 connect=yes listen=yes local.address=2001:db8::3 .role=ibgp name="Lokogoma V6" output.default-originate=never remote.address=2001:db8::2/128 .as=328160 router-id=198.51.100.97 routing-table=main
add address-families=ipv6 as=328160 connect=yes listen=yes local.address=2001:db8::6 .role=ibgp name="Eagle FM V6" output.default-originate=never remote.address=2001:db8::7/128 .as=328160 router-id=198.51.100.97 routing-table=main
add as=328160 connect=yes input.filter=medallion-in listen=yes local.address=198.51.100.97 .role=ibgp name=Abl-Medallion output.network=bgp-networks .no-client-to-client-reflection=yes remote.address=192.0.2.93/32 .as=328160 router-id=198.51.100.97 routing-table=main templates=default
/snmp
set contact=user@example.invalid enabled=yes location=abuja src-address=198.51.100.97 trap-generators=interfaces,start-trap trap-target=192.0.2.1 trap-version=2
/system identity
set name="Gudu Access"
/system ntp client
set enabled=yes
/system ntp client servers
add address=192.0.2.92
