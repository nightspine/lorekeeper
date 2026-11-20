"""Common English words, lowercased, for Ask's guards: a word in this set is never a name, so an answer may
use it without the index having said it, and a question may use it without it counting as unknown.

`BASE` holds the words (function words, everyday verbs, nouns and adjectives that summaries use, discourse
words such as however or meanwhile, numbers, days and months). `COMMON` adds the regular inflections of each
(s, es, ed, d, ing, er, est, ly, with a doubled final consonant), so the list needs no stemmer. It holds no
names: a word that is mostly a name (mark, bill, frank, rose) is left out on purpose.
"""

from __future__ import annotations

BASE = """
a able about above absent accept accident accompany according account accuse across act action active actually add
address admit adult advance advice afraid after afternoon afterwards again against age agree ahead aim air alarm alive
all allow almost alone along already also although always amid among amount ancient and anger angry animal announce
another answer anxious any anyone anything anyway anywhere apart appear appearance approach area argue arm army around
arrange arrest arrival arrive art as ashamed ask asleep assure at attack attempt attend attention aunt authority
autumn avoid awake aware away awful baby back bad bag ball band bank bar bare base battle be beach bear beast beat
beautiful because become bed before beg begin behave behind belief believe bell belong below beneath beside besides
best better between beyond big bird birth bit bite bitter black blame blind blood blow blue board boat body bone book
born borrow both bother bottle bottom bound box boy branch brave bread break breakfast breath bridge brief bright bring
broad broken brother brown build building burn burst bury business busy but buy by cage call calm camp can cannot
capture care careful carriage carry case castle catch cause cave cease cell centre century certain chain chair chance
change chapter character charge chase cheap check chief child childhood choice choose church circle city claim class
clean clear clever climb close cloth clothes cloud cold collect colour come comfort command common company compare
complete concern condition confess confirm confront confuse connect consider contain continue control conversation
cook cool corner correct cost could council count country couple courage course court cousin cover crazy create creature
crime cross crowd cruel cry cup cure curious curse cut damage dance danger dark darkness date daughter day dead deal
dear death decide declare deep defeat defend delay deliver demand deny depart describe deserve desire desperate destroy
detail develop devil die difference different difficult dig dinner direct direction dirty disappear discover discuss
disease dishonest distance distant disturb divide do doctor dog dollar done door doubt down dozen drag drama draw dream
dress drink drive drop drown dry due dull during dust duty each eager ear early earn earth east easy eat edge effect
effort egg either elder else emerge empty end enemy enemies enjoy enough enter entire entirely entrance equal escape
especially estate even evening event ever every everyone everything everywhere evidence evil exactly examine example
except exchange excited exclaim excuse exist expect experience explain explore express extra extreme eye face fact
fail faint fair faith fall false familiar family famous far farm fashion fast fat fate father fault fear feast feel
feet fellow female few field fierce fight figure fill final finally find fine finger finish fire firm first fish fit
fix flame flat flee flesh float floor flow flower fly follow food fool foot for force foreign forest forget forgive
form former forth fortune forward found frame free freedom fresh friend friendly frighten from front fruit full fun
funeral further future gain game garden gate gather gave general gentle gentleman get ghost gift girl give glad glance
glass go goal god gold gone good govern grand grandfather grandmother grass grave gray great green greet grew grey
ground group grow guard guess guest guide guilty gun hair half hall hand handle hang happen happy hard harm hat hate
have he head heal health hear heart heat heaven heavy held hell help her here hers herself hesitate hid hide high hill
him himself hire his history hit hold hole holy home honest honour hope horror horse hospital host hot hotel hour house
how however huge human hundred hungry hunt hunter hurry hurt husband i ice idea if ignore ill image imagine important
impossible in inch include indeed inform injure inn inside instead intend interest into invite iron is island it its
itself jail job join journey joy judge jump just keep key kill kind king kiss knee knew knife knock know knowledge
known lack lady lake lamp land language large last late later laugh law lay lead leader leaf learn least leave led
left leg length less lesson let letter level lie life lift light like likely limit line lion lip list listen little
live lock lone lonely long look lord lose loss lost lot loud love low loyal luck lunch mad made magic maid main make
male man manage manner many map march marry master match matter may maybe me meal mean meanwhile measure meat meet
member memory men mention mercy mere message met middle midnight might mile milk mind mine minute miss mission mistake
mix modern moment money monster month moon moral more morning most mother mountain mouth move much murder music must
my myself mystery name narrow nation native natural nature near nearby nearly neck need neighbour neither nervous
never new news next nice night no noble nobody noise none nor north nose not note nothing notice now number nurse
obey object observe obtain occasion occur of off offer office officer often oh old on once one only open opinion
opportunity or order ordinary other otherwise ought our ours ourselves out outside over owe own pack page pain pair
pale paper pardon parent park part particular partner party pass past path patient pay peace people perfect perhaps
period permit person pick picture piece pity place plain plan plant play please pleasant pleasure plenty plot point
poison police poor position possess possible post pour power practice praise pray prefer prepare presence present
press pretend pretty prevent price pride priest prince princess prison private prize probably problem promise proof
proper protect proud prove provide public pull punish pure purpose push put quarrel queen question quick quiet quite
race rage rain raise ran rank rare rather reach read ready real realise realize reason receive recent recognise
recognize record red refuse regard region regret relation remain remark remember remind remove repeat reply report
require rescue resist respect rest result return reveal reward rich ride right ring rise risk river road rob rock
role roll room root rope rough round row royal rule run rush sacrifice sad safe sail sake salt same sand sat save saw
say scarcely scene school science scream sea search season seat second secret section see seek seem seen seize self
sell send sense sent sentence serious servant serve service set settle several shadow shake shall shame shape share
sharp she sheep shelter shine ship shock shoot shop shore short should shoulder shout show shut sick side sight sign
silence silent silver similar simple sin since sing single sink sir sister sit situation size skin sky slave sleep
slight slip slow small smell smile smoke snow so social soft soil sold soldier solid some somebody somehow someone
something sometimes somewhere son song soon sorrow sorry sort soul sound south space speak special speech speed spend
spirit spite split spoke spread spring square staff stage stair stand star start state station stay steal steep step
stick still stir stone stood stop store storm story straight strange stranger stream street strength stress stretch
strike string strong struggle student study stuff stupid subject succeed such sudden suddenly suffer suggest suit
summer sun supply support suppose sure surface surprise surround suspect suspicion swear sweet swim sword system
table tail take tale talk tall task taste teach tear tell temper tend term terrible terror test than thank that the
their theirs them themselves then there therefore these they thick thin thing think third this those though thought
thousand threat three through throw thus tie tight till time tiny tire title to today together told tomorrow tone
tonight too took tool top total touch toward tower town trace track trade train travel treat tree trial tribe trick
trip trouble true trust truth try turn twice two type uncle under understand undo unhappy unknown unless until unusual
up upon upper upset us use usual valley value various vast very victim victory view village visit voice vow wait wake
walk wall want war warm warn was wash waste watch water wave way we weak wealth weapon wear weather week weep weight
welcome well went were west wet what whatever wheel when whenever where whereas wherever whether which while whisper
white who whole whom whose why wicked wide wife wild will willing win wind window wine wing winter wipe wise wish
witch with within without witness woman women won wonder wood word wore work world worry worse worst worth would
wound wrap write wrong yard year yellow yes yesterday yet you young your yours yourself zero
abandon ability aboard abruptly abuse acquire adopt adventure affair affect afford agent ago agreement aid alert
alliance allied alter alternative amaze ambush anchor angel announce ankle anniversary annoy apart apologise apology
apparent appeal appoint approve arch arise armour arrow ascend aside aspect assault assemble assist associate assume
atmosphere attach attract audience author available average await award aware axe bandage banner barely bargain barn
barrel basket bath bay beard bearing beg beloved bend benefit betray bind bishop blade blanket bless blessing blink
bloody blossom blush boil bold bolt bond bonus booth border bore boss bow bowl brain brand brass breed breeze bribe
brick bride brilliant brink brother broke brush bubble bucket bullet bunch burden burial bush butter button cabin
cable calculate candle cap capable captain captive capture card career carpet cart carve cast casual cattle cavern
celebrate cellar ceremony chamber champion chapel charm chest chin chip choke chorus chose chosen circumstance
citizen civil clan clap clasp claw clay cliff clock cloak closet clue coach coast coat code coffin coin collapse
college comrade concept conclude confidence conflict conquer conscience consent consequence constant contact contest
contract contrast convince copy cord cottage cotton cough counsel counter courtyard crack craft crash crawl creep
crew crimson crisis crop crown crush crystal cunning curtain custom dagger dare dash dawn deaf dealer debt decay deck
decline decorate decree deed deer definite degree delicate delight demon depend depth descend desert design desk
despair destiny determine devote dim dine dip disaster discipline disgrace disguise dish dismiss display dispute
distress ditch dive divine document dome domain donkey dose doubtful downstairs dragon drain drawer dread drew drift
drill drunk duke dungeon dwell eagle earnest echo edition educate elbow elect elegant element elevate emperor empire
employ enable enclose encounter encourage endure energy engage engine enormous ensure entertain envy equip era erect
error estimate eternal evade evident exact exceed excellent exhaust exile expense explode exposed extend fabric
faculty fade fairy fancy fare farewell fascinate fatal favour feather feature fee feed fence ferry festival fever
fiction fifty fifth filthy fist flag flash fled fleet flight fling flock flood flour flung flute foe fog fold folk
fond foolish forbid forehead forgot forgotten formal fort fortress forty fought foul fountain fourth fraction fragile
frequent friction fright frost frown froze frozen fuel fugitive fully furious furniture fury gallery gallop
gang gap gaze gear gem generation generous genius gesture giant glance glimpse glitter globe gloom glorious glory
glove glow goat goblin golden gossip gown grab graceful grain grant grasp gratitude gravel greatly grief grim grin
grip groan growl guarantee guardian guilt gum habit hammer harbour hardly harsh harvest hasten hatred haunt hawk
hay hazard headquarters heap heavily hedge heel height heir hence herb hero heroic hers hidden highway hint hip
hobby hollow honey hood hook horn horrible horrid hostile hostage howl humble humour hunger hunting hurl hut ideal
identity idle imitate immediate immense impatient imply impress improve impulse incident income indicate individual
industry infant inherit initial injury ink inner innocent inquire insect insist inspect instant instinct instruct
insult intelligent intense intention interrupt interval interview intimate introduce invade invent invisible
involve isle ivory jacket jar jaw jealous jest jewel joke journal joyful jury justice justify keen kettle kidnap
kin kingdom kitchen kneel knight knit knot label labour ladder lance landlord landscape lane lantern lap largely
lash latter launch lawn layer lazy leadership leaning league leap leather lecture legal legend leisure lend lens
liberty library lid lightning limb linen link liquid literature livid load loaf loan local lodge lofty log lonesome
loose loot lovely lover lower luxury lying machine madness magnificent maiden mail major majority mansion mantle
marble margin marine market marriage marsh mass mast mate material mayor meadow meanwhile medal medicine
medieval melt mend mental merchant merely merit merry mess metal method midst mighty mild military mill mirror
mischief miserable misery mist mock mode moderate modest monk monument mood mortal moss motion motive mount mourn
mouse movement murmur muscle mutter mutual mysterious myth nail naked napkin narrative nasty neat necessary needle
negative nerve nest net network neutral nevertheless nobleman nod nonsense noon normal noted novel nowhere nun oak
oath obedience obey obstacle obvious ocean odd odour offence offend official oil omen onto opening operate oppose
opposite option orchard organ origin orphan outer outlaw outline oven overcome overhead overlook owner pace pad
pail palace palm panic parade paradise parcel parish parlour passage passenger passion pasture patch patience
patrol pattern pause paw pearl peasant peculiar peer penalty pencil penny pepper perceive perform peril permanent
persist personal persuade pet phrase physical pigeon pile pillar pillow pilot pin pine pipe pirate pit plague plank
plate platform plead pledge plunge pocket poem poet poetry pole polite pond pony pool pope popular port portion
portrait pose positive possession pot pound powder practical prayer preach precious prejudice premise preserve
president prevail previous prey primary prime principle print priority prisoner privilege process proclaim produce
profession professor profit progress project prominent promote pronounce property prophecy prophet proportion
propose prospect protest provision prudent pump pupil purchase purple pursue puzzle quality quantity quarter quest
quote rabbit radiant rail rainbow rally ranch range rapid rattle raven ray razor reaction realm rear rebel recall
receipt reception recover recruit reduce reflect reform refuge regular reign reject rejoice relate release relief
relieve religion reluctant rely remarkable remedy remote rent repair repent represent reputation request reserve
resident resign resolve resort resource response responsible restore restrain retire retreat reunion revenge
reverse review revolt rhythm ribbon rid riddle rifle rim rinse riot ripe rival roar roast robe rod rogue romance
roof rot rotten royalty rub ruby rude ruin rumour rural rust ruthless sack sacred saddle safety sage sailor
saint salary sample sanctuary satisfy sauce savage scale scandal scar scatter schedule scheme scholar scorn scout
scrap scratch screen screw script sculpture seal seam secure seed seldom select senate senior sensation sensible
sentinel separate sequence sergeant series session setting shabby shade shaft shallow shed shell sheriff shield
shift shiver shore shower shrink shrug sigh signal signature silk silly simply sincere sinful sip sketch skill
skull slain slam slap slaughter slay sled sleeve slender slice slide slope slumber smash smooth snake snap snatch
sneer sniff soak sober society sofa solemn solitary solution solve somewhat sophisticated sore soothe sought spade
span spare spark spear species spectacle spell sphere spider spill spin spine spit splash splendid spoil sponge
spoon sport spot spouse spray squad squeeze stab stable stack stain stake stale stalk stamp standard stare starve
steady steam steel steer stem stern stew stiff stimulate sting stitch stocking stolen stomach stoop storey strain
strand strap straw stray stripe stroke stroll structure stubborn studio stumble stun style submit substance subtle
suburb successful suck suffering sufficient sugar suicide sulphur summit summon sunlight sunset sunshine superior
supper supreme surely surgeon surrender survey survive swallow swamp swan swarm sway sweat sweep swell swift swing
symbol sympathy tablet tackle tale talent tangle tap target tax tea teacher tear telegram temple temporary tempt
tenant tender tent terms terribly territory textbook thankful theatre theme theory thief thigh thirst thorn
thorough threaten thrill throat throne thumb thunder ticket tide tidy timber timid tin tip tobacco toe toll tomb
tongue tool tooth torch torment toss tournament towel toy trail trait traitor tramp transfer transform transport
trap tray treasure treaty tremble tremendous trial triumph troop tropical trumpet trunk tub tube tune tunnel turkey
twilight twin twist tyrant ugly unable uncertain unconscious undertake uniform union unite universe unlike unlock
upstairs urge urgent usage useful useless utter vague vain valuable vanish vapour variety vast vault vehicle veil
vein velvet vengeance venture verse version vessel veteran vice vigorous villain vine violence violent virgin virtue
visible vision visitor vital vivid volume voluntary vote voyage wage wagon waist wander ward warden warrior wary
wealthy weary weave wedding weed weigh weird wheat whip whistle widow width wilderness will wisdom wit wizard woe
wolf wonderful wooden wool worm worship wreck wretched yard yell yield youth zeal
one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen
nineteen twenty thirty fifty sixty seventy eighty ninety hundred thousand million first second third fourth fifth
sixth seventh eighth ninth tenth half double triple
monday tuesday wednesday thursday friday saturday sunday january february march april june july august september
october november december christmas easter
mr mrs ms dr st
hometown homeland homes homeless whilst amongst thereafter thereby therein thereof thence hereby herein hereafter forthwith nonetheless notwithstanding
moreover furthermore consequently accordingly subsequently previously eventually ultimately immediately instantly
initially originally apparently presumably evidently obviously clearly certainly definitely possibly probably
perhaps merely simply solely partly mostly mainly largely entirely completely totally absolutely utterly fully
nearly almost hardly barely rarely seldom occasionally frequently constantly gradually quickly slowly carefully
quietly silently secretly openly publicly privately personally finally lastly firstly secondly thirdly
later earlier afterward afterwards beforehand meanwhile otherwise instead besides anyway anyhow somehow
regardless nevertheless nonetheless yet still already again once twice often sometimes always never ever
begins began begun beginning ends ended ending
am are been being did does doing done had has having was were
tells told telling says said saying sees seen seeing goes went gone going comes came coming gets got gotten getting
makes made making takes took taken taking gives gave given giving finds found finding knows knew known knowing
thinks thought thinking leaves left leaving feels felt feeling keeps kept keeping holds held holding brings brought
bringing sets setting stands stood standing runs ran running sits sat sitting lies lay lain lying lying rises risen rising falls fell fallen falling grows grew grown growing draws drew drawn drawing breaks broke broken breaking
speaks spoke spoken speaking writes wrote written writing reads reading hears heard hearing sends sent sending
spends spent spending builds built building buys bought buying catches caught catching teaches taught teaching
fights fought fighting wins won winning loses lost losing meets met meeting pays paid paying sells sold selling
shoots shot shooting sleeps slept sleeping steals stole stolen stealing strikes struck striking swears swore sworn
swimming throws threw thrown throwing wakes woke woken waking wears wore worn wearing weeps wept weeping
children men women people feet teeth mice lives wives knives wolves thieves
dies died dying lays laid laying hides hid hidden hiding bites bit bitten biting bleeds bled bleeding
binds bound binding blows blew blown blowing burns burnt burned burning buries buried burying
bear bears bore born borne bearing beat beats beaten beating bend bent bending bet bids bade bidden
cast casts casting cling clung clinging creep crept creeping deal dealt dealing dig dug digging
feed fed feeding flee fled fleeing fling flung forget forgot forgotten forgetting forgive forgave forgiven
freeze froze frozen freezing hang hung hanged hanging hit hits hitting hurt hurts hurting kneel knelt kneeling
lead led leading lend lent lending light lit lighting mean meant meaning ride rode ridden riding ring rang rung
ringing seek sought seeking shake shook shaken shaking shine shone shining shut shuts shutting sing sang sung
singing sink sank sunk sinking slide slid sliding spin spun spinning spit spat spitting split splits splitting
spread spreads spreading spring sprang sprung springing stick stuck sticking sting stung stinging strive strove
sweep swept sweeping swing swung swinging tear tore torn tearing understand understood understanding
upon whom whoever whomever wherever whichever whenever whatever
""".split()

_VOWELS = "aeiou"


def _forms(w: str) -> set[str]:
    out = {w}
    if len(w) < 3 or not w.isalpha():
        return out
    if w.endswith("e"):
        out |= {w + "s", w + "d", w[:-1] + "ing", w + "r", w + "st", w + "ly"}
    elif w.endswith("y") and w[-2] not in _VOWELS:
        out |= {w[:-1] + "ies", w[:-1] + "ied", w + "ing", w[:-1] + "ier", w[:-1] + "iest", w[:-1] + "ily"}
    else:
        out |= {w + "s", w + "ed", w + "ing", w + "er", w + "est", w + "ly"}
        if w.endswith(("s", "x", "z", "ch", "sh")):
            out.add(w + "es")
        if len(w) >= 3 and w[-1] not in _VOWELS + "wxy" and w[-2] in _VOWELS and w[-3] not in _VOWELS:
            out |= {w + w[-1] + "ed", w + w[-1] + "ing", w + w[-1] + "er", w + w[-1] + "est"}
    return out


def _build() -> frozenset[str]:
    words: set[str] = set()
    for w in BASE:
        words |= _forms(w)
    return frozenset(words)


COMMON: frozenset[str] = _build()
