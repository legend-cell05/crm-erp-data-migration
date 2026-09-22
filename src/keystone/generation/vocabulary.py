"""The vocabulary the synthetic Arcadia CRM is built from.

Kept apart from the generator so the generator reads as logic rather than as
a wall of strings, and so the shape of the invented company is visible in one
place: mid-market B2B, mostly French, a handful of European subsidiaries.
"""

from __future__ import annotations

COMPANY_STEMS: tuple[str, ...] = (
    "Argos",
    "Belvedere",
    "Carnot",
    "Delmas",
    "Estrella",
    "Fabrice",
    "Gavroche",
    "Helios",
    "Ibis",
    "Jouvence",
    "Kermadec",
    "Lauzun",
    "Mistral",
    "Novalis",
    "Oriflamme",
    "Pasteur",
    "Quintal",
    "Rivoli",
    "Soubise",
    "Tramontane",
    "Ulysse",
    "Vercors",
    "Wagram",
    "Xenon",
    "Ygrec",
    "Zephyr",
    "Ambroise",
    "Bonaparte",
    "Chardon",
    "Dauphine",
    "Eluard",
    "Ferrand",
    "Grenelle",
    "Hautefort",
    "Iseran",
    "Jonquille",
    "Kleber",
    "Lubéron",
    "Montalembert",
    "Nerval",
    "Ombrelle",
    "Pontoise",
    "Quercy",
    "Roussillon",
    "Sancerre",
    "Trocadero",
    "Uzes",
    "Valmy",
    "Wattignies",
    "Yzeure",
)

COMPANY_SUFFIXES: tuple[str, ...] = (
    "SA",
    "SAS",
    "SARL",
    "Group",
    "Industries",
    "Consulting",
    "Logistique",
    "Technologies",
    "& Cie",
    "Partners",
    "Systèmes",
    "Distribution",
)

CITIES: tuple[tuple[str, str, str], ...] = (
    # city, zip prefix, country as the legacy system stored it
    ("Paris", "750", "FR"),
    ("Lyon", "690", "FR"),
    ("Marseille", "130", "France"),
    ("Toulouse", "310", "FR"),
    ("Lille", "590", "FRANCE"),
    ("Bordeaux", "330", "FR"),
    ("Nantes", "440", "Fr."),
    ("Strasbourg", "670", "FR"),
    ("Rennes", "350", "FR"),
    ("Créteil", "940", "FR"),
    ("Bruxelles", "1000", "BE"),
    ("Anvers", "2000", "Belgique"),
    ("Genève", "1200", "CH"),
    ("Lausanne", "1000", "Suisse"),
    ("Luxembourg", "1900", "LU"),
    ("Milan", "201", "IT"),
    ("Madrid", "280", "ES"),
    ("Barcelone", "080", "Espagne"),
    ("Francfort", "603", "DE"),
    ("Munich", "803", "Allemagne"),
)

STREETS: tuple[str, ...] = (
    "rue de la République",
    "avenue Victor Hugo",
    "boulevard Haussmann",
    "rue Pasteur",
    "avenue du Général de Gaulle",
    "rue des Acacias",
    "place de la Mairie",
    "chemin des Vignes",
    "rue du Commerce",
    "zone industrielle des Chênes",
    "allée des Tilleuls",
    "quai de Seine",
)

FIRST_NAMES: tuple[str, ...] = (
    "Marie",
    "Jean",
    "Sophie",
    "Pierre",
    "Claire",
    "Nicolas",
    "Camille",
    "Julien",
    "Émilie",
    "Thomas",
    "Laura",
    "Antoine",
    "Céline",
    "Mehdi",
    "Fatima",
    "Olivier",
    "Inès",
    "Lucas",
    "Nadia",
    "Vincent",
    "Aurélie",
    "Karim",
    "Delphine",
    "Mathieu",
    "Sarah",
    "Étienne",
    "Chloé",
    "Bastien",
)

LAST_NAMES: tuple[str, ...] = (
    "Martin",
    "Bernard",
    "Dubois",
    "Thomas",
    "Robert",
    "Richard",
    "Petit",
    "Durand",
    "Leroy",
    "Moreau",
    "Simon",
    "Laurent",
    "Lefebvre",
    "Michel",
    "Garcia",
    "David",
    "Bertrand",
    "Roux",
    "Vincent",
    "Fournier",
    "Morel",
    "Girard",
    "André",
    "Lefèvre",
    "Mercier",
    "Blanc",
    "Guerin",
    "Boyer",
)

JOB_TITLES: tuple[str, ...] = (
    "Directeur Général",
    "Responsable Achats",
    "Directeur Financier",
    "Responsable IT",
    "Chef de Projet",
    "Directeur Commercial",
    "Responsable Qualité",
    "Acheteur",
    "DSI",
    "Assistante de Direction",
    "Responsable Logistique",
    "Contrôleur de Gestion",
)

# Industry codes as registered in ref_industry. The generator deliberately
# also emits a few codes that are NOT in this table.
INDUSTRIES: tuple[tuple[str, str], ...] = (
    ("IND", "Industrie manufacturière"),
    ("SVC", "Services aux entreprises"),
    ("RET", "Commerce de détail"),
    ("PUB", "Secteur public"),
    ("TRA", "Transport et logistique"),
    ("SAN", "Santé"),
    ("BTP", "Construction"),
    ("AGR", "Agroalimentaire"),
)

UNREGISTERED_INDUSTRIES: tuple[str, ...] = ("MED", "ENR", "XXX", "99")

# Sales stages, and the ones the application let people type anyway.
STAGES: tuple[tuple[str, str, str, str], ...] = (
    # code, label, is_won, is_closed
    ("NEW", "Nouveau", "N", "N"),
    ("QUAL", "Qualifié", "N", "N"),
    ("PROP", "Proposition envoyée", "N", "N"),
    ("NEGO", "Négociation", "N", "N"),
    ("WON", "Gagné", "Y", "Y"),
    ("LOST", "Perdu", "N", "Y"),
    ("ABD", "Abandonné", "N", "Y"),
)

UNREGISTERED_STAGES: tuple[str, ...] = ("HOLD", "PEND", "?", "")

ACTIVITY_TYPES: tuple[str, ...] = ("CALL", "MEET", "MAIL", "NOTE", "TASK")

ACTIVITY_SUBJECTS: tuple[str, ...] = (
    "Appel de suivi",
    "Point trimestriel",
    "Envoi de la proposition",
    "Relance devis",
    "Visite sur site",
    "Réunion de cadrage",
    "Demande de renseignements",
    "Réclamation qualité",
    "Renouvellement contrat",
    "Présentation nouvelle gamme",
)

DEAL_DESCRIPTIONS: tuple[str, ...] = (
    "Renouvellement contrat annuel",
    "Extension de licences",
    "Fourniture équipements atelier",
    "Prestation de maintenance",
    "Refonte du parc logiciel",
    "Marché cadre 3 ans",
    "Commande complémentaire",
    "Migration infrastructure",
    "Accompagnement conformité",
    "Lot 2 - déploiement régional",
)
