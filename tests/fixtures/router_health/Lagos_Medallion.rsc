# <date> by RouterOS 7.22.2
/interface pppoe-server server
add default-profile=default-encryption disabled=no interface="Internet Access Bridge" one-session-per-host=yes service-name=fastnet
add default-profile=default-encryption disabled=no interface="vlan201=> Nasarawa" one-session-per-host=yes service-name=Nasarawa
add default-profile=default-encryption disabled=no interface="vlan501=>NHIS_JOS" one-session-per-host=yes service-name=NHIS_JOS
add default-profile=default-encryption disabled=no interface="vlan500=>NHIS_Calabar" one-session-per-host=yes service-name=NHIS_Calabar
add default-profile=default-encryption disabled=no interface=*3B one-session-per-host=yes service-name="NHIS OYO 1"
add default-profile=default-encryption disabled=no interface=*3C one-session-per-host=yes service-name="NHIS OYO 2"
add default-profile=default-encryption disabled=no interface=*3D keepalive-timeout=1 one-session-per-host=yes service-name="NHIS OSUN1"
add default-profile=default-encryption disabled=no interface=*3E one-session-per-host=yes service-name="NHIS_OSUN 2"
add default-profile=default-encryption disabled=no interface="vlan506=>NHIS_Ogun1" one-session-per-host=yes service-name="NHIS_OGUN 1"
add default-profile=default-encryption disabled=no interface="vlan508=>NHIS Ondo" one-session-per-host=yes service-name="NHIS Ondo"
add default-profile=default-encryption disabled=no interface="vlan509=>NHIS Delta 1" one-session-per-host=yes service-name="NHIS Delta 1"
add default-profile=default-encryption disabled=no interface="vlan510=>NHIS Delta 2" one-session-per-host=yes service-name="NHIS Delta 2"
add default-profile=default-encryption interface="vlan1232=>Abule Egba" one-session-per-host=yes service-name=Abulu-Egba
add default-profile=default-encryption interface="vlan1234=>Apapa" one-session-per-host=yes service-name=Dotmac-Apapa
add default-profile=default-encryption interface=*48 one-session-per-host=yes service-name="Fastnet Illupeju"
add default-profile=default-encryption disabled=no interface="vlan512=>NHIS ENUGU" one-session-per-host=yes service-name="NHIS ENUGU"
add default-profile=default-encryption disabled=no interface="vlan202=>NHIS Awka" one-session-per-host=yes service-name="NHIS AWKA"
add default-profile=default-encryption disabled=no interface="vlan511=>NHIS YENAGOA" one-session-per-host=yes service-name="NHIS YENEGOA"
add default-profile=default-encryption disabled=no interface="vlan513=>NHIS Portharcourt" one-session-per-host=yes service-name="NHIS Portharcourt"
add default-profile=default-encryption disabled=no interface="vlan514=>NHIS Kaduna" one-session-per-host=yes service-name="NHIS Kaduna"
add default-profile=default-encryption disabled=no interface="vlan515=>NHIS Osun" one-session-per-host=yes service-name="NHIS Osun"
add default-profile=default-encryption disabled=no interface="vlan516=>NHIS Zamfara" one-session-per-host=yes service-name="NHIS Zamfara"
add default-profile=default-encryption disabled=no interface="vlan517=>NHIS Ibadan oyo" one-session-per-host=yes service-name="NHIS Ibadan oyo"
add default-profile=default-encryption disabled=no interface="vlan518=>NHIS Nassarawa" one-session-per-host=yes service-name="NHIS Nassarawa"
add default-profile=default-encryption disabled=no interface="vlan519=>NHIS Umuahia - ABIA" one-session-per-host=yes service-name="NHIS UMUAHIA - ABIA"
add default-profile=default-encryption disabled=no interface="vlan521=>NHIS Kebbi" one-session-per-host=yes service-name="NHIS Kebbi"
add default-profile=default-encryption disabled=no interface="vlan520=>NHIS Enugu" one-session-per-host=yes service-name="NHIS Enugu"
add default-profile=default-encryption disabled=no interface="vlan523=>NHIS Benin" one-session-per-host=yes service-name="NHIS Benin"
add default-profile=default-encryption disabled=no interface="vlan522=>NHIS Bauchi" one-session-per-host=yes service-name="NHIS Bauchi"
add default-profile=default-encryption disabled=no interface="vlan525=>NHIS Minna" one-session-per-host=yes service-name="NHIS Minna"
add default-profile=default-encryption disabled=no interface="vlan202=>NHIS Awka" one-session-per-host=yes service-name="NHIS Akwa"
add default-profile=default-encryption disabled=no interface="vlan526=>NHIS Uyo" one-session-per-host=yes service-name="NHIS Uyo"
add default-profile=default-encryption disabled=no interface="vlan527=>NHIS Kogi" one-session-per-host=yes service-name="NHIS Kogi"
add default-profile=default-encryption disabled=no interface="vlan528 =>NHIS OGUN" one-session-per-host=yes service-name="NHIS Ogun"
add default-profile=default-encryption disabled=no interface="vlan529=>NHIS Awka 2" one-session-per-host=yes service-name="NHIS AWKA 2"
add default-profile=default-encryption disabled=no interface="vlan530=>NIGCOMSAT ZARIA" one-session-per-host=yes service-name="NIGCOMSAT ZARIA"
add default-profile=default-encryption disabled=no interface="vlan531=>NIGCOMSAT ENUGU" one-session-per-host=yes service-name="NIGCOMSAT ENUGU"
add default-profile=default-encryption disabled=no interface="vlan555=>NIGCOMSAT OSUN" one-session-per-host=yes service-name="NIGCOMSAT OSUN"
add default-profile=default-encryption disabled=no interface="vlan532=>NIGCOMSAT KATSINA" one-session-per-host=yes service-name="NIGCOMSAT KATSINA"
add default-profile=default-encryption disabled=no interface="vlan300=>Maiduguri FAO" one-session-per-host=yes service-name="NHIS Maiduguri FAO"
add default-profile=default-encryption disabled=no interface="vlan1191=>FIRS_ Ibadan lto,oyo gbo" one-session-per-host=yes service-name="FIRS Ibadan lto"
add default-profile=default-encryption disabled=no interface="vlan1192=> FIRS_adeoyo eto,adeoyo eta" one-session-per-host=yes service-name="FIRS Adeoyo eto"
add default-profile=default-encryption disabled=no interface="vlan 2288=> FIRS_Abeokuta eto Abeokuta eta" one-session-per-host=yes service-name="FIRS Abeokuta eta"
add default-profile=default-encryption disabled=no interface="vlan1190=>FIRS_ state cordination" one-session-per-host=yes service-name="FIRS State coordination"
add default-profile=default-encryption disabled=no interface="vlan1392=>FIRS_ondo gbo,akure eto" one-session-per-host=yes service-name="FIRS Ondo Akure eto"
add default-profile=default-encryption disabled=no interface="vlan1194=> FIRS_osun gbo,oshgbo eta osun" one-session-per-host=yes service-name="FIRS Osun Osogbo"
add default-profile=default-encryption disabled=no interface="vlan1816=>FIRS_ota eto,ota eta oyo" one-session-per-host=yes service-name="FIRS Ota Eto"
add default-profile=default-encryption disabled=no interface="vlan3534=>FIRS_Ekiti gbo,ado-ekiti eto" one-session-per-host=yes service-name="FIRS Ekiti Gbo"
add default-profile=default-encryption disabled=no interface="vlan 2277=>FIRS_ shagamu eto" one-session-per-host=yes service-name="FIRS Shagamu eto"
add default-profile=default-encryption disabled=no interface="vlan1990=>FIRS_kwara gbo ilorin eto." one-session-per-host=yes service-name="FIRS Kwara gbo"
add default-profile=default-encryption disabled=no interface="vlan1820=>FIRS_oyo eto" one-session-per-host=yes service-name="FIRS Oyo eto"
add default-profile=default-encryption disabled=no interface="vlan1991=>FIRS_ore eto" one-session-per-host=yes service-name="FIRS ore eto"
add default-profile=default-encryption disabled=no interface="vlan1391=>FIRS_Keo sco, ondo" one-session-per-host=yes service-name="FIRS Keo sco"
add default-profile=default-encryption disabled=no interface="vlan1193=>FIRS_Iwo road eto,mto,ibadan" one-session-per-host=yes service-name="FIRS Iwo road eto mto ibadan"
add default-profile=default-encryption disabled=no interface="vlan1992=>FIRS_owo eto" one-session-per-host=yes service-name="FIRS owo eto"
/ip firewall address-list
add address=192.0.2.85/22 comment="Dotmac Abuja" list=allowed-mgmt
add address=192.0.2.86/22 comment="Dotmac Lagos" list=allowed-mgmt
add address=192.0.2.87/24 comment="Server farm" list=allowed-mgmt
add address=192.0.2.88/12 comment="Internal management" list=allowed-mgmt
add address=198.51.100.98 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.99 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.100 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.101 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.102 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.103 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.104 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.105 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.106 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.107 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.108 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.109 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.110 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.111 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.112 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.113 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.114 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.115 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.116 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.117 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.118 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.119 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.120 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.121 comment="dotmac migrated after app/radius reconcile" list=suspended
add address=198.51.100.122 comment="dotmac migrated after app/radius reconcile" list=suspended
/ip firewall filter
add action=accept chain=forward comment="Allow Ilupeju router syslog to local Zabbix" dst-address=192.0.2.1 dst-port=514 protocol=udp src-address=198.51.100.123
add action=jump chain=forward comment="dotmac suspended quarantine" dst-address-list=!splynx-allowed-resources jump-target=dotmac-suspended src-address-list=suspended
add action=accept chain=splynx-blocked comment="dotmac portal allow (suspended)" dst-address=192.0.2.3 dst-port=80,443 protocol=tcp
add action=accept chain=input comment="INPUT-PROTECT: SSH from VPN monitoring" dst-port=120 protocol=tcp src-address=198.51.100.124/24
add action=fasttrack-connection chain=forward comment="FastTrack established/related" connection-state=established,related
add action=accept chain=input disabled=yes src-address-list=whitelist
add action=accept chain=input comment="Allow management from trusted external IP" dst-port=120,1256,443,8291 protocol=tcp src-address=198.51.100.125
add action=accept chain=input comment="Allow management from trusted external IP" dst-port=120,1256,443,8291 protocol=tcp src-address=198.51.100.126
add action=accept chain=input dst-port=8291 protocol=tcp src-address=198.51.100.127
add action=accept chain=input comment="Allow WireGuard wg-dotmac" dst-port=51820 protocol=udp
add action=drop chain=input comment="Drop external Winbox scans" dst-port=8291 protocol=tcp src-address-list=!allowed-mgmt
add action=accept chain=dotmac-suspended comment="dotmac suspended dns allow" dst-port=53 protocol=udp
add action=accept chain=dotmac-suspended comment="dotmac suspended portal allow" dst-address=192.0.2.3 dst-port=80,443 protocol=tcp
add action=reject chain=dotmac-suspended comment="dotmac suspended reject" reject-with=icmp-admin-prohibited
add action=drop chain=dotmac-suspended comment="dotmac suspended drop"
/ip firewall mangle
/ip firewall nat
/ip service
set ftp disabled=yes
set telnet disabled=yes
set ssh address="192.0.2.85/22,192.0.2.86/22,192.0.2.87/24,192.0.2.88/12,192.0.2.3/32" port=120
set www-ssl address=192.0.2.3/32,192.0.2.89/32 certificate=REDACTED disabled=no
set www address=192.0.2.85/22,192.0.2.86/22,192.0.2.87/24,192.0.2.88/12 disabled=yes port=1256
set winbox address=192.0.2.85/22,192.0.2.86/22,192.0.2.87/24,192.0.2.88/12
set api address=192.0.2.3/32
set api-ssl disabled=yes
/ipv6 firewall filter
add action=drop chain=input comment="Block unsolicited RAs from IXP" disabled=yes icmp-options=134:0 in-interface="sfpplus3=>IXPN Lagos" protocol=icmpv6
/radius
add address=192.0.2.3 comment="dotmac_sub mirror" service=ppp,hotspot src-address=198.51.100.128 timeout=3s
add accounting-port=1823 address=192.0.2.3 authentication-port=1822 comment="dotmac device-login" service=login src-address=198.51.100.128 timeout=3s
/radius incoming
set accept=yes
/routing bgp connection
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=google-in instance=bgp-instance-1 listen=yes local.role=ebgp name=Google output.filter-chain="Goggle private-out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.129/32 .as=15169 .port=179 routing-table=main templates="Dotmac BGP"
add as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=lagos-ixp-in instance=bgp-instance-1 listen=yes local.address=198.51.100.130 .role=ebgp name="IXP Pri" output.filter-chain=ixpn-out .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.131/32 .as=36932 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=lagos-ixp-in instance=bgp-instance-1 listen=yes local.role=ebgp name="IXP Sec" output.filter-chain=ixpn-out .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.132/32 .as=36932 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.role=ebgp name=Verisign output.filter-chain="Dotmac Private-Out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.133/32 .as=26415 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.address=198.51.100.130 .role=ebgp name=Web4Africa output.filter-chain="Dotmac Private-Out" .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.134/32 .as=327813 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.role=ebgp name=PCH output.filter-chain="Dotmac Private-Out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.135/32 .as=42 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.role=ebgp name=PCH-2 output.filter-chain="Dotmac Private-Out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.136/32 .as=3856 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=facebook-in instance=bgp-instance-1 listen=yes local.role=ebgp name=Facebook-1 output.filter-chain="Facebook private-out" .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.137/32 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=facebook-in instance=bgp-instance-1 listen=yes local.role=ebgp name=Facebook-2 output.filter-chain="Facebook private-out" .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.138/32 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=facebook-in instance=bgp-instance-1 listen=yes local.role=ebgp name="Facebook-1 V6" output.filter-chain="Private V6-Out" .network="bgp V6" .no-client-to-client-reflection=yes .redistribute=bgp remote.address=2001:db8::8/128 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=facebook-in instance=bgp-instance-1 listen=yes local.role=ebgp name="Facebook-2 V6" output.filter-chain="Private V6-Out" .network="bgp V6" .no-client-to-client-reflection=yes .redistribute=bgp remote.address=2001:db8::9/128 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=google-in instance=bgp-instance-1 listen=yes local.role=ebgp name="Google V6" output.filter-chain="Private V6-Out" .network="bgp V6" .no-client-to-client-reflection=yes remote.address=2001:db8::a/128 .as=15169 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=Nigcomsat-in instance=bgp-instance-1 listen=yes local.role=ebgp name=Nigcomsat output.default-originate=if-installed .filter-chain="Default Gateway" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.139/32 .as=37472 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=lagos-ixp-in instance=bgp-instance-1 listen=yes local.address=2001:db8::b .role=ebgp name="IXP V6" output.filter-chain="Private V6-Out" .network=IPv6_Prefixes .no-client-to-client-reflection=yes remote.address=2001:db8::c/128 .as=36932 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.address=198.51.100.128 .role=ibgp name="Abuja Core Garki" output.network=bgp-networks .no-client-to-client-reflection=yes .redistribute="" remote.address=192.0.2.92/32 .as=328160 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.address=2001:db8::d .role=ibgp name="Abuja Core Garki V6" output.network="bgp V6" .no-client-to-client-reflection=yes .redistribute="" remote.address=2001:db8::e/128 .as=328160 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.address=198.51.100.130 .role=ebgp name="Liquid Inteli-Tech-V4" output.filter-chain="Dotmac Private-Out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.140/32 .as=30844 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.role=ebgp name="Liquid Inteli-Tech-V6" output.filter-chain="Private V6-Out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=2001:db8::f/128 .as=30844 .port=179 routing-table=main templates="Dotmac BGP"
add as=328160 connect=yes disabled=no instance=bgp-instance-1 listen=yes local.address=198.51.100.128 .role=ibgp name="CSS Access" output.default-originate=always .network=bgp-networks .no-client-to-client-reflection=yes .redistribute="" remote.address=198.51.100.141/32 .as=328160 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no instance=bgp-instance-1 listen=yes local.address=198.51.100.142 .role=ebgp name=GlobalCom output.filter-chain="Glo out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.143/32 .as=37148 .port=179 routing-table=main templates="Dotmac BGP"
add connect=yes disabled=no instance=bgp-instance-1 listen=yes local.address=198.51.100.128 .role=ibgp name="Apapa Access" output.default-originate=always .network=bgp-networks .no-client-to-client-reflection=yes .redistribute="" remote.address=198.51.100.144/32 .as=328160 .port=179 routing-table=main templates="Dotmac BGP"
add connect=yes disabled=no instance=bgp-instance-1 listen=yes local.address=198.51.100.128 .role=ibgp multihop=yes name="Surulere Access" output.default-originate=if-installed .network=bgp-networks .no-client-to-client-reflection=yes .redistribute="" remote.address=198.51.100.145/32 .as=328160 .port=179 routing-table=main templates="Dotmac BGP"
add as=328160 connect=yes disabled=yes instance=bgp-instance-1 listen=yes local.address=198.51.100.128 .role=ibgp multihop=yes name=Dopemu output.default-originate=always .network=bgp-networks .no-client-to-client-reflection=yes .redistribute="" remote.address=198.51.100.146/32 .as=328160 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=tiktok-in instance=bgp-instance-1 listen=yes local.role=ebgp name=TikTok-1 output.filter-chain="Tiktok private-out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.147/32 .as=396986 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=tiktok-in instance=bgp-instance-1 listen=yes local.role=ebgp name=TikTok-2 output.filter-chain="Tiktok private-out" .network=bgp-networks .no-client-to-client-reflection=yes remote.address=198.51.100.148/32 .as=396986 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=tiktok-in instance=bgp-instance-1 listen=yes local.role=ebgp name="TikTok-1 V6" output.filter-chain="Private V6-Out" .network=IPv6_Prefixes .no-client-to-client-reflection=yes remote.address=2001:db8::10/128 .as=396986 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=tiktok-in instance=bgp-instance-1 listen=yes local.role=ebgp name="TikTok-2 V6" output.filter-chain="Private V6-Out" .network="bgp V6" .no-client-to-client-reflection=yes remote.address=2001:db8::11/128 .as=396986 .port=179 routing-table=main templates="Dotmac BGP"
add cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter="Default Gateway" instance=bgp-instance-1 listen=yes local.address=198.51.100.149 .role=ebgp name=AngolaCables output.filter-chain=Angola_out .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.150/32 .as=37468 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 connect=yes instance=bgp-instance-1 listen=yes local.address=2001:db8::12 .role=ibgp name=CSS-V6 output.default-originate=always .network="bgp V6" remote.address=2001:db8::13/128 .as=328160 routing-table=main
add afi=ipv6 connect=yes instance=bgp-instance-1 listen=yes local.address=2001:db8::14 .role=ibgp multihop=yes name=Allen-V6 nexthop-choice=force-self output.default-originate=always .network="bgp V6" remote.address=2001:db8::15/128 .as=328160 routing-table=main
add afi=ipv6 connect=yes instance=bgp-instance-1 listen=yes local.address=2001:db8::14 .role=ibgp multihop=yes name=Ilupeju-V6 nexthop-choice=force-self output.default-originate=always .network="bgp V6" remote.address=2001:db8::16/128 .as=328160 routing-table=main
add afi=ipv6 connect=yes instance=bgp-instance-1 listen=yes local.address=2001:db8::14 .role=ibgp multihop=yes name=Surulere-V6 nexthop-choice=force-self output.default-originate=always .network="bgp V6" remote.address=2001:db8::17/128 .as=328160 routing-table=main
add afi=ipv6 connect=yes instance=bgp-instance-1 listen=yes local.address=2001:db8::12 .role=ibgp multihop=yes name=Apapa-V6 output.default-originate=always .network="bgp V6" remote.address=2001:db8::18/128 .as=328160 routing-table=main
add afi=ipv6 connect=yes instance=bgp-instance-1 listen=yes local.address=2001:db8::12 .role=ibgp multihop=yes name=Dopemu-V6 output.default-originate=always .network="bgp V6" remote.address=2001:db8::19/128 .as=328160 routing-table=main
add connect=yes instance=bgp-instance-1 listen=yes local.address=198.51.100.128 .role=ibgp multihop=yes name="Allen Router" output.default-originate=always .network=bgp-networks .no-client-to-client-reflection=yes .redistribute="" remote.address=198.51.100.151/32 routing-table=main templates="Dotmac BGP"
add connect=yes instance=bgp-instance-1 listen=yes local.address=198.51.100.128 .role=ibgp multihop=yes name="Ilupeju Access" output.default-originate=always .network=bgp-networks .no-client-to-client-reflection=yes .redistribute="" remote.address=198.51.100.123/32 routing-table=main templates="Dotmac BGP"
add afi=ipv6 as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=meta-in instance=bgp-instance-1 listen=yes local.role=ebgp name="Meta-1 V6" output.filter-chain="Private V6-Out" .network="bgp V6" .no-client-to-client-reflection=yes .redistribute=bgp remote.address=2001:db8::1a/128 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=meta-in instance=bgp-instance-1 listen=yes local.role=ebgp name="Meta-2 V6" output.filter-chain="Private V6-Out" .network="bgp V6" .no-client-to-client-reflection=yes .redistribute=bgp remote.address=2001:db8::1b/128 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=meta-in instance=bgp-instance-1 listen=yes local.role=ebgp name="Meta-3 V6" output.filter-chain="Private V6-Out" .network="bgp V6" .no-client-to-client-reflection=yes .redistribute=bgp remote.address=2001:db8::1c/128 routing-table=main templates="Dotmac BGP"
add afi=ipv6 as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=meta-in instance=bgp-instance-1 listen=yes local.role=ebgp name="Meta-4 V6" output.filter-chain="Private V6-Out" .network="bgp V6" .no-client-to-client-reflection=yes .redistribute=bgp remote.address=2001:db8::1d/128 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=meta-in instance=bgp-instance-1 listen=yes local.role=ebgp name=Meta-1 output.filter-chain="Meta private-out" .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.152/32 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=meta-in instance=bgp-instance-1 listen=yes local.role=ebgp name=Meta-2 output.filter-chain="Meta private-out" .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=ospf remote.address=198.51.100.153/32 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=meta-in instance=bgp-instance-1 listen=yes local.role=ebgp name=Meta-3 output.filter-chain="Meta private-out" .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.154/32 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add as=328160 cisco-vpls-nlri-len-fmt=auto-bits connect=yes disabled=no input.filter=meta-in instance=bgp-instance-1 listen=yes local.role=ebgp name=Meta-4 output.filter-chain="Meta private-out" .network=bgp-networks .no-client-to-client-reflection=yes .redistribute=bgp remote.address=198.51.100.155/32 .as=32934 .port=179 routing-table=main templates="Dotmac BGP"
add afi=ipv6 as=328160 comment="Angola Cables IPv6 transit - Lagos" input.filter=angola-v6-in .limit-process-routes-ipv6=500000 instance=bgp-instance-1 local.address=2001:db8::1e .role=ebgp name="AngolaCables V6" output.filter-chain="Private V6-Out" .network=IPv6_Prefixes .redistribute=bgp remote.address=2001:db8::1f/128 .as=37468 routing-table=main templates="Dotmac BGP"
/snmp
set contact=user@example.invalid enabled=yes location=abuja trap-generators=interfaces,start-trap trap-target=192.0.2.1 trap-version=2
/system identity
set name="Lagos Medallion"
/system ntp client
set enabled=yes
/system ntp client servers
add address=time.google.com
add address=pool.ntp.org
