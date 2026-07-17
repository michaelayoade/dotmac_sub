"""NCC location reference data — canonical states, LGAs, towns, FCT districts.

Reference tables for the NCC returns: the 36 states + FCT + the INTERNATIONAL
bucket, their 775 Local Government Areas, the accepted town list, and the FCT
area-council/district/town table. Their job is to **validate and canonicalise a
location that was captured**, never to infer one.

Deliberately NOT ported from CRM: its address→location guessing chain
(``_map_ncc_location`` and friends). That code scanned a free-text address for
any recognisable fragment and, failing that, defaulted the complaint to
``Municipal Area Council, FEDERAL CAPITAL TERRITORY`` — inventing a location for
a regulatory filing. Location must be captured through its owning service, not
guessed here. If a captured value does not resolve, these functions return
``""``/``None`` and the caller reports the gap.

Reused from ``ncc_subscriber_report`` rather than duplicated:
``normalize_state`` (with its ``_STATE_CANON``/``_STATE_ALIASES`` handling for
"abuja"/"fct"/"akwa-ibom"/…) and ``_normalize_location_key``. Verified
equivalent to CRM's ``_normalize_ncc_region`` and its LGA-key expression across
all 1,565 table names and 775 LGA keys — zero disagreements — so reuse cannot
shift a filed number. Sub's alias table is richer than CRM's one-entry
``{"FCT": ...}``, so free-text like "Abuja" now canonicalises instead of being
rejected; that is canonicalisation of a captured value, not inference.
"""

from __future__ import annotations

from app.services.ncc_subscriber_report import (
    _normalize_location_key,
    normalize_state,
)

_UNKNOWN_STATE = "Unknown"

# ── Input hygiene ───────────────────────────────────────────────────────────
_NCC_EMPTY_MARKERS = {
    "-",
    "--",
    "---",
    "n/a",
    "na",
    "nil",
    "none",
    "null",
    "unknown",
    "not available",
    "not applicable",
    "not specified",
}


def _clean_basic_text(value: object) -> str:
    """Trim/collapse a captured value; placeholder markers count as empty."""
    cleaned = " ".join(str(value or "").strip().split())
    if cleaned.lower() in _NCC_EMPTY_MARKERS:
        return ""
    return cleaned


# ── States and LGAs ─────────────────────────────────────────────────────────
_NCC_STATE_LGAS: dict[str, tuple[str, ...]] = {
    "ABIA": (
        "Aba North",
        "Aba South",
        "Arochukwu",
        "Bende",
        "Ikwuano",
        "Isiala-Ngwa North",
        "Isiala-Ngwa South",
        "Isuikwuato",
        "Obi Ngwa",
        "Ohafia",
        "Osisioma Ngwa",
        "Ugwunagbo",
        "Ukwa East",
        "Ukwa West",
        "Umuahia North",
        "Umuahia South",
        "Umu-Nneochi",
    ),
    "ADAMAWA": (
        "Demsa",
        "Fufore",
        "Ganye",
        "Girei",
        "Gombi",
        "Guyuk",
        "Hong",
        "Jada",
        "Lamurde",
        "Madagali",
        "Maiha",
        "Mayo-Belwa",
        "Michika",
        "Mubi North",
        "Mubi South",
        "Numan",
        "Shelleng",
        "Song",
        "Toungo",
        "Yola North",
        "Yola South",
    ),
    "AKWA IBOM": (
        "Abak",
        "Eastern Obolo",
        "Eket",
        "Esit-Eket",
        "Essien Udim",
        "Etim Ekpo",
        "Etinan",
        "Ibeno",
        "Ibesikpo Asutan",
        "Ibiono-Ibom",
        "Ika",
        "Ikono",
        "Ikot Abasi",
        "Ikot Ekpene",
        "Ini",
        "Itu",
        "Mbo",
        "Mkpat-Enin",
        "Nsit-Atai",
        "Nsit-Ibom",
        "Nsit-Ubium",
        "Obot Akara",
        "Okobo",
        "Onna",
        "Oron",
        "Oruk Anam",
        "Udung-Uko",
        "Ukanafun",
        "Uruan",
        "Urue-Offong/Oruko",
        "Uyo",
    ),
    "ANAMBRA": (
        "Aguata",
        "Anambra East",
        "Anambra West",
        "Anaocha",
        "Awka North",
        "Awka South",
        "Ayamelum",
        "Dunukofia",
        "Ekwusigo",
        "Idemili North",
        "Idemili South",
        "Ihiala",
        "Njikoka",
        "Nnewi North",
        "Nnewi South",
        "Ogbaru",
        "Onitsha North",
        "Onitsha South",
        "Orumba North",
        "Orumba South",
        "Oyi",
    ),
    "BAUCHI": (
        "Alkaleri",
        "Bauchi",
        "Bogoro",
        "Damban",
        "Darazo",
        "Dass",
        "Gamawa",
        "Ganjuwa",
        "Giade",
        "Itas/Gadau",
        "Jama'are",
        "Katagum",
        "Kirfi",
        "Misau",
        "Ningi",
        "Shira",
        "Tafawa Balewa",
        "Toro",
        "Warji",
        "Zaki",
    ),
    "BAYELSA": (
        "Brass",
        "Ekeremor",
        "Kolokuma/Opokuma",
        "Nembe",
        "Ogbia",
        "Sagbama",
        "Southern Ijaw",
        "Yenagoa",
    ),
    "BENUE": (
        "Ado",
        "Agatu",
        "Apa",
        "Buruku",
        "Gboko",
        "Guma",
        "Gwer East",
        "Gwer West",
        "Katsina-Ala",
        "Konshisha",
        "Kwande",
        "Logo",
        "Makurdi",
        "Obi",
        "Ogbadibo",
        "Ohimini",
        "Oju",
        "Okpokwu",
        "Otukpo",
        "Tarka",
        "Ukum",
        "Ushongo",
        "Vandeikya",
    ),
    "BORNO": (
        "Abadam",
        "Askira/Uba",
        "Bama",
        "Bayo",
        "Biu",
        "Chibok",
        "Damboa",
        "Dikwa",
        "Gubio",
        "Guzamala",
        "Gwoza",
        "Hawul",
        "Jere",
        "Kaga",
        "Kala/Balge",
        "Konduga",
        "Kukawa",
        "Kwaya Kusar",
        "Mafa",
        "Magumeri",
        "Maiduguri",
        "Marte",
        "Mobbar",
        "Monguno",
        "Ngala",
        "Nganzai",
        "Shani",
    ),
    "CROSS RIVER": (
        "Abi",
        "Akamkpa",
        "Akpabuyo",
        "Bakassi",
        "Bekwarra",
        "Biase",
        "Boki",
        "Calabar Municipal",
        "Calabar South",
        "Etung",
        "Ikom",
        "Obanliku",
        "Obubra",
        "Obudu",
        "Odukpani",
        "Ogoja",
        "Yakuur",
        "Yala",
    ),
    "DELTA": (
        "Aniocha North",
        "Aniocha South",
        "Bomadi",
        "Burutu",
        "Ethiope East",
        "Ethiope West",
        "Ika North-East",
        "Ika South",
        "Isoko North",
        "Isoko South",
        "Ndokwa East",
        "Ndokwa West",
        "Okpe",
        "Oshimili North",
        "Oshimili South",
        "Patani",
        "Sapele",
        "Udu",
        "Ughelli North",
        "Ughelli South",
        "Ukwuani",
        "Uvwie",
        "Warri North",
        "Warri South",
        "Warri South West",
    ),
    "EBONYI": (
        "Abakaliki",
        "Afikpo North",
        "Afikpo South",
        "Ebonyi",
        "Ezza North",
        "Ezza South",
        "Ikwo",
        "Ishielu",
        "Ivo",
        "Izzi",
        "Ohaozara",
        "Ohaukwu",
        "Onicha",
    ),
    "EDO": (
        "Akoko-Edo",
        "Egor",
        "Esan Central",
        "Esan North-East",
        "Esan South-East",
        "Esan West",
        "Etsako Central",
        "Etsako East",
        "Etsako West",
        "Igueben",
        "Ikpoba-Okha",
        "Oredo",
        "Orhionmwon",
        "Ovia North-East",
        "Ovia South-West",
        "Owan East",
        "Owan West",
        "Uhunmwonde",
    ),
    "EKITI": (
        "Ado-Ekiti",
        "Efon",
        "Ekiti East",
        "Ekiti South-West",
        "Ekiti West",
        "Emure",
        "Gbonyin",
        "Ido-Osi",
        "Ijero",
        "Ikere",
        "Ikole",
        "Ilejemeje",
        "Irepodun/Ifelodun",
        "Ise/Orun",
        "Moba",
        "Oye",
    ),
    "ENUGU": (
        "Aninri",
        "Awgu",
        "Enugu East",
        "Enugu North",
        "Enugu South",
        "Ezeagu",
        "Igbo-Etiti",
        "Igbo-Eze North",
        "Igbo-Eze South",
        "Isi-Uzo",
        "Nkanu East",
        "Nkanu West",
        "Nsukka",
        "Oji-River",
        "Udenu",
        "Udi",
        "Uzo-Uwani",
    ),
    "FEDERAL CAPITAL TERRITORY": (
        "Abaji",
        "Bwari",
        "Gwagwalada",
        "Kuje",
        "Kwali",
        "Municipal Area Council",
    ),
    "GOMBE": (
        "Akko",
        "Balanga",
        "Billiri",
        "Dukku",
        "Funakaye",
        "Gombe",
        "Kaltungo",
        "Kwami",
        "Nafada",
        "Shongom",
        "Yamaltu/Deba",
    ),
    "IMO": (
        "Aboh-Mbaise",
        "Ahiazu-Mbaise",
        "Ehime-Mbano",
        "Ezinihitte",
        "Ideato North",
        "Ideato South",
        "Ihitte/Uboma",
        "Ikeduru",
        "Isiala Mbano",
        "Isu",
        "Mbaitoli",
        "Ngor-Okpala",
        "Njaba",
        "Nkwerre",
        "Nwangele",
        "Obowo",
        "Oguta",
        "Ohaji/Egbema",
        "Okigwe",
        "Onuimo",
        "Orlu",
        "Orsu",
        "Oru East",
        "Oru West",
        "Owerri Municipal",
        "Owerri North",
        "Owerri West",
    ),
    "JIGAWA": (
        "Auyo",
        "Babura",
        "Biriniwa",
        "Birnin Kudu",
        "Buji",
        "Dutse",
        "Gagarawa",
        "Garki",
        "Gumel",
        "Guri",
        "Gwaram",
        "Gwiwa",
        "Hadejia",
        "Jahun",
        "Kafin Hausa",
        "Kaugama",
        "Kazaure",
        "Kiri Kasama",
        "Kiyawa",
        "Maigatari",
        "Malam Madori",
        "Miga",
        "Ringim",
        "Roni",
        "Sule-Tankarkar",
        "Taura",
        "Yankwashi",
    ),
    "KADUNA": (
        "Birnin Gwari",
        "Chikun",
        "Giwa",
        "Igabi",
        "Ikara",
        "Jaba",
        "Jema'a",
        "Kachia",
        "Kaduna North",
        "Kaduna South",
        "Kagarko",
        "Kajuru",
        "Kaura",
        "Kauru",
        "Kubau",
        "Kudan",
        "Lere",
        "Makarfi",
        "Sabon Gari",
        "Sanga",
        "Soba",
        "Zangon Kataf",
        "Zaria",
    ),
    "KANO": (
        "Ajingi",
        "Albasu",
        "Bagwai",
        "Bebeji",
        "Bichi",
        "Bunkure",
        "Dala",
        "Dambatta",
        "Dawakin Kudu",
        "Dawakin Tofa",
        "Doguwa",
        "Fagge",
        "Gabasawa",
        "Garko",
        "Garum Mallam",
        "Gaya",
        "Gezawa",
        "Gwale",
        "Gwarzo",
        "Kabo",
        "Kano Municipal",
        "Karaye",
        "Kibiya",
        "Kiru",
        "Kumbotso",
        "Kunchi",
        "Kura",
        "Madobi",
        "Makoda",
        "Minjibir",
        "Nasarawa",
        "Rano",
        "Rimin Gado",
        "Rogo",
        "Shanono",
        "Sumaila",
        "Takai",
        "Tarauni",
        "Tofa",
        "Tsanyawa",
        "Tudun Wada",
        "Ungogo",
        "Warawa",
        "Wudil",
    ),
    "KATSINA": (
        "Bakori",
        "Batagarawa",
        "Batsari",
        "Baure",
        "Bindawa",
        "Charanchi",
        "Dan Musa",
        "Dandume",
        "Danja",
        "Daura",
        "Dutsi",
        "Dutsin-Ma",
        "Faskari",
        "Funtua",
        "Ingawa",
        "Jibia",
        "Kafur",
        "Kaita",
        "Kankara",
        "Kankia",
        "Katsina",
        "Kurfi",
        "Kusada",
        "Mai'adua",
        "Malumfashi",
        "Mani",
        "Mashi",
        "Matazu",
        "Musawa",
        "Rimi",
        "Sabuwa",
        "Safana",
        "Sandamu",
        "Zango",
    ),
    "KEBBI": (
        "Aleiro",
        "Arewa Dandi",
        "Argungu",
        "Augie",
        "Bagudo",
        "Birnin Kebbi",
        "Bunza",
        "Dandi",
        "Fakai",
        "Gwandu",
        "Jega",
        "Kalgo",
        "Koko/Besse",
        "Maiyama",
        "Ngaski",
        "Sakaba",
        "Shanga",
        "Suru",
        "Wasagu/Danko",
        "Yauri",
        "Zuru",
    ),
    "KOGI": (
        "Adavi",
        "Ajaokuta",
        "Ankpa",
        "Bassa",
        "Dekina",
        "Ibaji",
        "Idah",
        "Igalamela-Odolu",
        "Ijumu",
        "Kabba/Bunu",
        "Kogi",
        "Lokoja",
        "Mopa-Muro",
        "Ofu",
        "Ogori/Magongo",
        "Okehi",
        "Okene",
        "Olamaboro",
        "Omala",
        "Yagba East",
        "Yagba West",
    ),
    "KWARA": (
        "Asa",
        "Baruten",
        "Edu",
        "Ekiti",
        "Ifelodun",
        "Ilorin East",
        "Ilorin South",
        "Ilorin West",
        "Irepodun",
        "Isin",
        "Kaiama",
        "Moro",
        "Offa",
        "Oke-Ero",
        "Oyun",
        "Pategi",
    ),
    "LAGOS": (
        "Agege",
        "Ajeromi-Ifelodun",
        "Alimosho",
        "Amuwo-Odofin",
        "Apapa",
        "Badagry",
        "Epe",
        "Eti-Osa",
        "Ibeju-Lekki",
        "Ifako-Ijaiye",
        "Ikeja",
        "Ikorodu",
        "Kosofe",
        "Lagos Island",
        "Lagos Mainland",
        "Mushin",
        "Ojo",
        "Oshodi-Isolo",
        "Shomolu",
        "Surulere",
    ),
    "NASARAWA": (
        "Akwanga",
        "Awe",
        "Doma",
        "Karu",
        "Keana",
        "Keffi",
        "Kokona",
        "Lafia",
        "Nasarawa",
        "Nasarawa Egon",
        "Obi",
        "Toto",
        "Wamba",
    ),
    "NIGER": (
        "Agaie",
        "Agwara",
        "Bida",
        "Borgu",
        "Bosso",
        "Chanchaga",
        "Edati",
        "Gbako",
        "Gurara",
        "Katcha",
        "Kontagora",
        "Lapai",
        "Lavun",
        "Magama",
        "Mariga",
        "Mashegu",
        "Mokwa",
        "Moya",
        "Paikoro",
        "Rafi",
        "Rijau",
        "Shiroro",
        "Suleja",
        "Tafa",
        "Wushishi",
    ),
    "OGUN": (
        "Abeokuta North",
        "Abeokuta South",
        "Ado-Odo/Ota",
        "Egbado North",
        "Egbado South",
        "Ewekoro",
        "Ifo",
        "Ijebu East",
        "Ijebu North",
        "Ijebu North-East",
        "Ijebu Ode",
        "Ikenne",
        "Imeko-Afon",
        "Ipokia",
        "Obafemi-Owode",
        "Odeda",
        "Odogbolu",
        "Ogun Waterside",
        "Remo North",
        "Shagamu",
    ),
    "ONDO": (
        "Akoko North-East",
        "Akoko North-West",
        "Akoko South-East",
        "Akoko South-West",
        "Akure North",
        "Akure South",
        "Ese-Odo",
        "Idanre",
        "Ifedore",
        "Ilaje",
        "Ile-Oluji/Okeigbo",
        "Irele",
        "Odigbo",
        "Okitipupa",
        "Ondo East",
        "Ondo West",
        "Ose",
        "Owo",
    ),
    "OSUN": (
        "Atakumosa East",
        "Atakumosa West",
        "Ayedade",
        "Ayedire",
        "Boluwaduro",
        "Boripe",
        "Ede North",
        "Ede South",
        "Egbedore",
        "Ejigbo",
        "Ife Central",
        "Ife East",
        "Ife North",
        "Ife South",
        "Ifedayo",
        "Ifelodun",
        "Ila",
        "Ilesa East",
        "Ilesa West",
        "Irepodun",
        "Irewole",
        "Isokan",
        "Iwo",
        "Obokun",
        "Odo-Otin",
        "Ola-Oluwa",
        "Olorunda",
        "Oriade",
        "Orolu",
        "Osogbo",
    ),
    "OYO": (
        "Afijio",
        "Akinyele",
        "Atiba",
        "Atisbo",
        "Egbeda",
        "Ibadan North",
        "Ibadan North-East",
        "Ibadan North-West",
        "Ibadan South-East",
        "Ibadan South-West",
        "Ibarapa Central",
        "Ibarapa East",
        "Ibarapa North",
        "Ido",
        "Irepo",
        "Iseyin",
        "Itesiwaju",
        "Iwajowa",
        "Kajola",
        "Lagelu",
        "Ogbomosho North",
        "Ogbomosho South",
        "Ogo Oluwa",
        "Olorunsogo",
        "Oluyole",
        "Ona-Ara",
        "Orelope",
        "Ori-Ire",
        "Oyo East",
        "Oyo West",
        "Saki East",
        "Saki West",
        "Surulere",
    ),
    "PLATEAU": (
        "Barkin Ladi",
        "Bassa",
        "Bokkos",
        "Jos East",
        "Jos North",
        "Jos South",
        "Kanam",
        "Kanke",
        "Langtang North",
        "Langtang South",
        "Mangu",
        "Mikang",
        "Pankshin",
        "Qua'an Pan",
        "Riyom",
        "Shendam",
        "Wase",
    ),
    "RIVERS": (
        "Abua/Odual",
        "Ahoada East",
        "Ahoada West",
        "Akuku-Toru",
        "Andoni",
        "Asari-Toru",
        "Bonny",
        "Degema",
        "Eleme",
        "Emohua",
        "Etche",
        "Gokana",
        "Ikwerre",
        "Khana",
        "Obio/Akpor",
        "Ogba/Egbema/Ndoni",
        "Ogu/Bolo",
        "Okrika",
        "Omuma",
        "Opobo/Nkoro",
        "Oyigbo",
        "Port Harcourt",
        "Tai",
    ),
    "SOKOTO": (
        "Binji",
        "Bodinga",
        "Dange-Shuni",
        "Gada",
        "Goronyo",
        "Gudu",
        "Gwadabawa",
        "Illela",
        "Isa",
        "Kebbe",
        "Kware",
        "Rabah",
        "Sabon Birni",
        "Shagari",
        "Silame",
        "Sokoto North",
        "Sokoto South",
        "Tambuwal",
        "Tangaza",
        "Tureta",
        "Wamako",
        "Wurno",
        "Yabo",
    ),
    "TARABA": (
        "Ardo-Kola",
        "Bali",
        "Donga",
        "Gashaka",
        "Gassol",
        "Ibi",
        "Jalingo",
        "Karim-Lamido",
        "Kumi",
        "Lau",
        "Sardauna",
        "Takum",
        "Ussa",
        "Wukari",
        "Yorro",
        "Zing",
    ),
    "YOBE": (
        "Bade",
        "Bursari",
        "Damaturu",
        "Fika",
        "Fune",
        "Geidam",
        "Gujba",
        "Gulani",
        "Jakusko",
        "Karasuwa",
        "Machina",
        "Nangere",
        "Nguru",
        "Potiskum",
        "Tarmuwa",
        "Yunusari",
        "Yusufari",
    ),
    "ZAMFARA": (
        "Anka",
        "Bakura",
        "Birnin Magaji/Kiyaw",
        "Bukkuyum",
        "Bungudu",
        "Gummi",
        "Gusau",
        "Kaura Namoda",
        "Maradun",
        "Maru",
        "Shinkafi",
        "Talata-Mafara",
        "Tsafe",
        "Zurmi",
    ),
    "INTERNATIONAL": ("International",),
}


_LGA_LOOKUP_BY_STATE: dict[str, dict[str, str]] = {
    state: {_normalize_location_key(lga): lga for lga in lgas}
    for state, lgas in _NCC_STATE_LGAS.items()
}


# ── Towns ───────────────────────────────────────────────────────────────────
_NCC_ACCEPTED_TOWNS = (
    "Angawa Bawa",
    "Gbagarape",
    "Kugbo",
    "Nyanya Site-Area A-F",
    "Nyanya Village/Gwandara",
    "Nyanya Village/Gwari",
    "Asokoro",
    "Garki",
    "Jabi",
    "Lokogoma",
    "Maitama",
    "Abacha Barracks",
    "Apo",
    "Damagaza",
    "Dantata",
    "Durumi I",
    "Durumi II",
    "Durumi III",
    "Dutse",
    "Garki Village",
    "Gudu",
    "Guzape",
    "Kobi",
    "Kurumduma",
    "NEPA Village",
    "Wumba",
    "Gui",
    "Airport",
    "Barowa",
    "Damakuba",
    "Dandi",
    "Dayisa",
    "Dodo",
    "Gbenduniya",
    "Gbessa",
    "Gora",
    "Gosa",
    "Gud Pasali",
    "Gwako",
    "Iddo Maaji",
    "Iddo Pada",
    "Iddo Sabo",
    "Iddo Sarki",
    "Iddo Tudunwada",
    "Koloke",
    "Makana",
    "Makanima",
    "Nuwalogye",
    "Sauka",
    "Takilogo",
    "Toge",
    "Tunga Kwaso",
    "Tungan Jika",
    "Tungan Wakili Isa",
    "Zamani",
    "Bagusa",
    "Dei-Dei",
    "Filin Dabo",
    "Filin Dabo I",
    "Filin Dabo II",
    "Gwagwa",
    "Kaba",
    "Kagini",
    "Karsana I",
    "Karsana II",
    "Karsana III",
    "Saburi I",
    "Saburi II",
    "Tasha",
    "Zaudna",
    "Aleyita",
    "Burum",
    "Dogori Gada",
    "Galadimawa",
    "Kabusa",
    "Ketti",
    "Lekugoma",
    "Lugbe",
    "Piwoyi",
    "Pykasa",
    "Sabon Lugbe",
    "Sheretti",
    "Takushara",
    "Wani",
    "Zhidu",
    "Zidna",
    "Gwarinpa Fed. Housing",
    "Gwarinpa Life Camp",
    "Gwarinpa Village",
    "Kado Federal Housing",
    "Kado Village",
    "Katampe",
    "Kuchigoro",
    "Mabushi",
    "Utako",
    "Ajata",
    "Angwan Sako",
    "Anka",
    "Badna",
    "Chori Bisa",
    "Gidan Ajiya",
    "Gidan Mangoro",
    "Gugugu",
    "Kpepegyi",
    "Kurudu",
    "Kurudu Gwandara",
    "Kwoi",
    "Madalla",
    "Munapeyi Kasa",
    "Munapeyi Sama",
    "Orozo I",
    "Orozo II",
    "Sabon Gari",
    "Wowo",
    "Jikoyi",
    "Karu Site (FHA)",
    "Karu Village (FHA)",
)


_NCC_TOWN_ALIASES = {
    "garki 2": "Garki",
    "garki ii": "Garki",
    "garki area 2": "Garki",
    "garki district": "Garki",
    "garki village": "Garki Village",
    "gwarimpa fed housing": "Gwarinpa Fed. Housing",
    "gwarinpa fed housing": "Gwarinpa Fed. Housing",
    "gwarimpa federal housing": "Gwarinpa Fed. Housing",
    "gwarinpa federal housing": "Gwarinpa Fed. Housing",
    "gwarimpa life camp": "Gwarinpa Life Camp",
    "gwarinpa life camp": "Gwarinpa Life Camp",
    "gwarimpa village": "Gwarinpa Village",
    "gwarinpa village": "Gwarinpa Village",
    "jikwoyi": "Jikoyi",
    "kpeyegyi": "Kpepegyi",
    "nepa village": "NEPA Village",
}


_TOWN_LOOKUP: dict[str, str] = {
    _normalize_location_key(town): town for town in _NCC_ACCEPTED_TOWNS
}
_TOWN_LOOKUP.update(_NCC_TOWN_ALIASES)


# ── FCT area councils / districts / towns ───────────────────────────────────
def _town_tuple(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(";") if part.strip())


_NCC_FCT_DISTRICT_ROWS = (
    (
        "Municipal Area Council",
        "Garki",
        (
            "Abacha Barracks",
            "Apo",
            "Damagaza",
            "Dantata",
            "Durumi I",
            "Durumi II",
            "Durumi III",
            "Dutse",
            "Garki Village",
            "Gudu",
            "Guzape",
            "Kobi",
            "Kurumduma",
            "NEPA Village",
            "Wumba",
        ),
    ),
    (
        "Municipal Area Council",
        "Gui",
        (
            "Airport",
            "Barowa",
            "Damakuba",
            "Dandi",
            "Dayisa",
            "Dodo",
            "Gbenduniya",
            "Gbessa",
            "Gora",
            "Gosa",
            "Gud Pasali",
            "Gui",
            "Gwako",
            "Iddo Maaji",
            "Iddo Pada",
            "Iddo Sabo",
            "Iddo Sarki",
            "Iddo Tudunwada",
            "Koloke",
            "Makana",
            "Makanima",
            "Nuwalogye",
            "Sauka",
            "Takilogo",
            "Toge",
            "Tunga Kwaso",
            "Tungan Jika",
            "Tungan Wakili Isa",
            "Zamani",
        ),
    ),
    (
        "Municipal Area Council",
        "Gwagwa",
        (
            "Bagusa",
            "Dei-die",
            "Filin Dabo",
            "Filin Dabo I",
            "Filin Dabo II",
            "Gwagwa",
            "Kaba",
            "Kagini",
            "Karsana I",
            "Karsana II",
            "Karsana III",
            "Saburi I",
            "Saburi II",
            "Tasha",
            "Zaudna",
        ),
    ),
    (
        "Municipal Area Council",
        "Gwarinpa",
        (
            "Gwarinpa Fed. Housing",
            "Gwarinpa Life Camp",
            "Gwarinpa Village",
            "Kado Federal Housing",
            "Kado Village",
            "Katampe",
            "Kuchigoro",
            "Mabushi",
            "Utako",
        ),
    ),
    (
        "Municipal Area Council",
        "Jiwa",
        (
            "Basan Jiwa",
            "Gyeda",
            "Hulumi",
            "Idu",
            "Idu Gwari",
            "Jiwa",
            "Karmo Sabo",
            "Karmo Tsoho",
            "Paipe",
            "Tungan Dallatu",
            "Tungan Madaki",
            "Zhidu",
        ),
    ),
    (
        "Municipal Area Council",
        "Kabusa",
        (
            "Aleyita",
            "Burum",
            "Dogori Gada",
            "Galadimawa",
            "Kabusa",
            "Ketti",
            "Lokogoma",
            "Lugbe",
            "Piwoyi",
            "Pykasa",
            "Sabon Lugbe",
            "Sheretti",
            "Takushara",
            "Wani",
            "Zhidu",
            "Zidna",
        ),
    ),
    (
        "Municipal Area Council",
        "Karu",
        (
            "Jikwoyi",
            "Karu Site (FHA)",
            "Karu Village",
        ),
    ),
    (
        "Municipal Area Council",
        "Nyanya",
        (
            "Angawa Bawa",
            "Gbagarape",
            "Kugbo",
            "Nyanya Site-Area A-F",
            "Nyanya Village/Gwandara",
            "Nyanya Village/Gwari",
        ),
    ),
    (
        "Municipal Area Council",
        "Orozo",
        (
            "Ajata",
            "Angwan Sako",
            "Anka",
            "Badna",
            "Chori Bisa",
            "Gidan Ajiya",
            "Gidan Mangoro",
            "Gugugu",
            "Kpepegyi",
            "Kurudu",
            "Kurudu Gwandara",
            "Kwoi",
            "Madalla",
            "Munapeyi Kasa",
            "Munapeyi Sama",
            "Orozo I",
            "Orozo II",
            "Sabon Gari",
            "Wowo",
        ),
    ),
    (
        "Abaji",
        "Abaji",
        _town_tuple(
            "Agyana; Bago; Bandagi; Dapala; Ebagi; Gbogbogo; Kebba; Manderegi; Nah. Tosho; "
            "Nahalati Sabo; Nuku; Panagana; Rimba; Uboshenu; Yawule"
        ),
    ),
    (
        "Abaji",
        "Yaba",
        _town_tuple(
            "Abuja; Adagba; Afo; Akori; Alampa; Allu; Ayaba; Bari-Bezi; Bazi-Bezi; Busga; "
            "Chakun; Chundugo; Dabbare; Dara; Dewu; Domi; Dum; Madechi; Ekki; Fakon Tando; "
            "Gadabiri; Gari; Gasakba; Gasukpa; Gawu; Gawun; Gidan Maisaye; Gurdi; Guruza; "
            "Gwanda; Gwona; Jamigbe; Kafako; Kpace; Kularida; Kutara; Kwago; Kwakwa; Kyawu; "
            "Lafia Yaba; Managi; Nadichi; Nagun; Nassarawa; Nowog; Nyembo; Pako Base; Panagu; "
            "Pandaji; Pankuru; Panpari; Piowe; Sabongida; Sarowo-Abdu; Selifulyu; Shadad; "
            "Soitan; Takpeshi; Talpa; Tanaga; Wapa; Yelwa; Yelwa Gawu; Zuwa"
        ),
    ),
    (
        "Bwari",
        "Bwari",
        _town_tuple(
            "Apugye; Barago; Baran Rafi; Barangoni; Barapa; Bazango Bwari; Bunko; Byazhi; "
            "Chikale; Dankoru; Dauda; Donabayi; Duba; Dutse Alhaji; Gaba; Galuwyi; "
            "Gidan Babachi; Gidan Baushe; Gidan Pawa; Gudupe; Gutpo; Igu; Jigo; Kaima; "
            "Karaku; Karawa; Kasaru; Katampe; Kawadashi; Kawu; Kikumi; Kimtaru; Kogo; Kubwa; "
            "Kuchibuyi; Kuduru; Kurumin Daudu; Kute; Kwabwure; Panda; Panunuki; Paspa; Payi; "
            "Piko; Rugan S/Fulani; Ruriji; Sabon Gari; Sagwari; Shere; Simape; Sumpe; "
            "T/Danzaria; T/Manu; Tokulo; Tudun Wada; Tunga Bijimi; Tunga-Adoka; Tungan Sarkin; "
            "Ushafa; Yaba; Yajida; Yaupe; Yayidna; Zango; Zuma"
        ),
    ),
    (
        "Gwagwalada",
        "Gwagwalada",
        _town_tuple(
            "Agota; Akwayi; Akyakyata; Anguwar Hausawa; Anguwar Sarki; Atopi; Bargada Bassa; "
            "Basan Zuba; Bassa; Biyu; Boka; Chaboda; Chitumu; Dabagayi; Dada; Dada Gongo; "
            "Damin Kara; Dawaki; Diko; Dobi; Gidan Ango; Gidan Bala; Gidan Dandu; Gidan Gade; "
            "Goi; Gongo; Gurebare; Gwako; Gwale; Gwari; Gwagwalada; Ibwa; Ibwa Sarki; Ikwa; "
            "Kaburufi; Kace Bassa; Kace Sabo; Kace Sarki; Kaida Bassa; Kaida Gwari; Kalangu; "
            "Kasanki; Kutunku; Lafiya; Ledi; Makama; Paiko; Pako; Paso Gwari; Sabon Gari; "
            "Shaga I; Shaga II; Shida; Soko; Tungan Adamu; Tungan Auta; Tungan Giwa; "
            "Tungan Jika; Tungan Salihu; Wuma; Wumi; Zuba"
        ),
    ),
    (
        "Kuje",
        "Kuje",
        _town_tuple(
            "Damwa; Achmbi; Aduga; Agwai; Atsauna; Baban Kurmi; Bamishi; Banayi II; "
            "Barayi Pada; Bugako; Buzunkure; Chegasu; Chibiri; Chida; Chukuku; Dafara; "
            "Damakusa; Damangata; Dubia; Duma; Duriya; Gafere Sabo; Ganagu Sabo; Gando; "
            "Gashe; Gaube; Gawu; Gbebasa; Gidan Jatau; Gidan-Bawa; Gwari; Iye; Jeli; Kabi; "
            "Kabikasa; Kamo G.; Kanzo; Kapa; Kasada; Kiyi; Kuja Pada; Kusaki; Kutada; "
            "Kwaku; Lafiya; Lafiya Gwari; Lamiga; Madatta; Mogada; Nufawa; Pasali; Peki; "
            "Pima; Rubokya; Sabon Gwaria; Sauka; Shaji; Takwa Gwari; Takwa Hausawa; Tukpeki; "
            "Tukuba I; Tukuba II; Wumi; Yalwa; Yamma; Yanga; Zangon Kara; Zilu"
        ),
    ),
    (
        "Kuje",
        "Rubochi",
        _town_tuple(
            "Adegbe; Affa; Ahinza; Attako; Bida; Buga; Darika; Gabiya; Gidan Bawa; Gombe; "
            "Gova; Gudun Karya; Gwagwada; Gyana; Huni-Gade; Huni-Gwari; Kujekwa; Kule; "
            "Kutunbwa; Mabamade; Munu; Odun Bisa; Odun Kasa; Perri; Rubatu; Rubochi; Rugese; "
            "Sabe; Sungba; Tika; Tuturutu; Ukya; Ungwar-Madaki; Ure; Yaba; Yewusa; Zagabutu; "
            "Zoge; Zokutu"
        ),
    ),
    (
        "Kwali",
        "Ashara",
        _town_tuple(
            "2 Tudu; Ahuwye; Akapo; Angun Tunga; Angun Wakili; Angun Woji Woji; Ashara; "
            "Bassoni; Bodolo; Chekanci; Daganaruwa Bassa; Damakusa Gwari; Daniwayo; Eke; "
            "Gomani; Gorgbe; Gulo; Gwaji; Gwan auta; Huton; Janruwa; Kona Mada; Kpessili; "
            "Kukka; Kukka Bushe; Kundu Lele; Kunguni; Maikwari; Mumun; Nboni; Nzakpara; "
            "Padama; Puka; Pukafa; Rarra; Riwaza; Sabo Gari Gurara; Sadaba; Sharra; "
            "T. Sarki; Takuro Mallan; Tekpesse; Tudu Wada Mangu"
        ),
    ),
    (
        "Kwali",
        "Dafa",
        _town_tuple(
            "Azaya; Dafa; Dafa SaboDaji; Galo; Gugwa; Kangon Adamu; Kpewuye; Kye; Puka; Tungan Galadima; Tungan Gani; Tungan Guli; Tungan Tofa"
        ),
    ),
    (
        "Kwali",
        "Gumbo",
        _town_tuple(
            "Anini; Elle; Gidan Duniya; Gidan Makaniki; Gumbo; Kamadi; Kwaita Hausa; Lukoda; Piri; Shepikati; Tusun Fulani; Tutubwa"
        ),
    ),
    (
        "Kwali",
        "Kilankwa",
        _town_tuple(
            "Chukuku; Kilankwa I; Kilankwa II; Petti; Sheda Galadima; Sheda Sarki"
        ),
    ),
    (
        "Kwali",
        "Kwali",
        _town_tuple(
            "Bonugo; Dafara; Ebo; Farakuti; Farakuti I; Farakuti II; Fulani; Kigbe; Koda; Kwaida Tsoho; Kwaita Sabo; Kwali; Lambata; Leda; Police Barracks; Rugan Mal. Idris; Rugan Rabo; Sarki; Yambabu"
        ),
    ),
    (
        "Kwali",
        "Pai",
        _town_tuple(
            "Bako; Bobota; Ceceyi; Dabi; Kuti Chichi; Leleyi; Leleyi Bassa; Pai Fulani; Pai Gwari; Tatu; Tukurwa"
        ),
    ),
    (
        "Kwali",
        "Wako",
        _town_tuple(
            "(Ubosharu); Anguwar Baushe; Awawa; Azarachi; Bukpe; Chida; Dangara; Dapa; Gadabiyu; Kibuyi; Sa'adu; Sabon Gari; Ubo Saidu; Wako; Yewuti"
        ),
    ),
    (
        "Kwali",
        "Yangoji",
        _town_tuple(
            "Adadu 1; Adadu 11; Bwoto; Daka; Ijah Dabuta; Ijah Sarki; Koroki; Kuyi; Nitse; Sukuku; Tampe; Yangoji"
        ),
    ),
    ("Kwali", "Yebu", _town_tuple("Ebo; Kigbe; Yebu")),
)


_FCT_LOCATION_LOOKUP: dict[str, tuple[str, str]] = {}
for _lga, _district, _towns in _NCC_FCT_DISTRICT_ROWS:
    _FCT_LOCATION_LOOKUP[_normalize_location_key(_district)] = (_lga, _district)
    for _town in _towns:
        _normalized_town = _normalize_location_key(_town)
        if _normalized_town == "abuja":
            continue
        _FCT_LOCATION_LOOKUP[_normalized_town] = (_lga, _town)


_FCT_LOCATION_ALIASES = {
    "dei dei": ("Municipal Area Council", "Dei-die"),
    "garki 2": ("Municipal Area Council", "Garki"),
    "garki ii": ("Municipal Area Council", "Garki"),
    "garki area 2": ("Municipal Area Council", "Garki"),
    "gwarimpa": ("Municipal Area Council", "Gwarinpa"),
    "gwarimpa fed housing": ("Municipal Area Council", "Gwarinpa Fed. Housing"),
    "gwarimpa federal housing": ("Municipal Area Council", "Gwarinpa Fed. Housing"),
    "gwarimpa life camp": ("Municipal Area Council", "Gwarinpa Life Camp"),
    "gwarimpa village": ("Municipal Area Council", "Gwarinpa Village"),
    "jahi": ("Municipal Area Council", "Jahi"),
    "jikwoyi": ("Municipal Area Council", "Jikwoyi"),
    "karsana": ("Municipal Area Council", "Karsana I"),
    "kpeyegyi": ("Municipal Area Council", "Kpepegyi"),
    "nepa village": ("Municipal Area Council", "Garki"),
    "wuse": ("Municipal Area Council", "Wuse"),
    "wuse zone 1": ("Municipal Area Council", "Wuse"),
    "wuse zone 2": ("Municipal Area Council", "Wuse"),
    "wuse zone 3": ("Municipal Area Council", "Wuse"),
    "wuse zone 4": ("Municipal Area Council", "Wuse"),
    "wuse zone 5": ("Municipal Area Council", "Wuse"),
    "zone 1": ("Municipal Area Council", "Wuse"),
    "zone 2": ("Municipal Area Council", "Wuse"),
    "zone 3": ("Municipal Area Council", "Wuse"),
    "zone 4": ("Municipal Area Council", "Wuse"),
    "zone 5": ("Municipal Area Council", "Wuse"),
}


_FCT_LOCATION_LOOKUP.update(_FCT_LOCATION_ALIASES)

# ── Public API ──────────────────────────────────────────────────────────────


def states() -> tuple[str, ...]:
    """Every NCC state label (36 states + FCT + the INTERNATIONAL bucket)."""
    return tuple(_NCC_STATE_LGAS)


def canonical_state(value: object) -> str:
    """A captured state → its NCC label, or ``""`` if it is not an NCC state.

    Never guesses: an unrecognised value is rejected, not defaulted.
    """
    cleaned = _clean_basic_text(value)
    if not cleaned:
        return ""
    upper = cleaned.upper()
    if upper in _NCC_STATE_LGAS:
        return upper
    # INTERNATIONAL is an NCC bucket, not a Nigerian state, so it is only ever
    # matched by the exact-label branch above.
    state = normalize_state(cleaned)
    if state == _UNKNOWN_STATE:
        return ""
    upper = state.upper()
    return upper if upper in _NCC_STATE_LGAS else ""


def lgas_for_state(state: object) -> tuple[str, ...]:
    """The LGAs of a state, or ``()`` when the state is unrecognised."""
    return _NCC_STATE_LGAS.get(canonical_state(state), ())


def canonical_lga(state: object, lga: object) -> str:
    """A captured LGA → its canonical spelling within ``state``, else ``""``."""
    key = _normalize_location_key(_clean_basic_text(lga))
    if not key:
        return ""
    return _LGA_LOOKUP_BY_STATE.get(canonical_state(state), {}).get(key, "")


def is_valid_lga(state: object, lga: object) -> bool:
    """True when ``lga`` is a real LGA of ``state``."""
    return bool(canonical_lga(state, lga))


def accepted_towns() -> tuple[str, ...]:
    """Every town the NCC return accepts."""
    return _NCC_ACCEPTED_TOWNS


def canonical_town(value: object) -> str:
    """A captured town → its accepted spelling, or ``""`` if not accepted."""
    key = _normalize_location_key(_clean_basic_text(value))
    if not key:
        return ""
    return _TOWN_LOOKUP.get(key, "")


def is_accepted_town(value: object) -> bool:
    """True when ``value`` names a town on the accepted list."""
    return bool(canonical_town(value))


def fct_location_for_town(value: object) -> tuple[str, str] | None:
    """A captured FCT town/district → ``(area_council, district_or_town)``.

    ``None`` when the town is not in the FCT table — the caller reports the
    gap rather than defaulting to an area council.
    """
    key = _normalize_location_key(_clean_basic_text(value))
    if not key:
        return None
    return _FCT_LOCATION_LOOKUP.get(key)


def fct_district_rows() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """The FCT table as ``(area_council, district, towns)`` rows."""
    return _NCC_FCT_DISTRICT_ROWS
