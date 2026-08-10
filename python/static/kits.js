/* Original kit graphics — drop-in replacement for the shirt PNGs that were
 * hotlinked from fantasy.premierleague.com.
 *
 * A generic jersey silhouette, drawn here, filled with each club's colours.
 * Club colours are facts about a club rather than protected expression, and
 * the shape is ours, so nothing is copied from anyone. It also removes a
 * runtime dependency on a URL we don't control: FPL can rename that path at
 * any time and every player card on the site would break with no warning.
 *
 * Loaded before app.js in index.html, because app.js calls shirtImg() while
 * rendering the pitch. main.py's asset_version() hashes this file too, so an
 * edit here busts the browser cache the same way a CSS or app.js change does.
 *
 * Codes are FPL `team_code`, not `id` — that's what app.js already passes.
 * Adding a promoted club is one row; nothing else needs touching.
 *
 * Fields:
 *   primary    body colour
 *   secondary  sleeve colour, and the stripe colour when pattern is 'stripes'
 *   pattern    'solid' | 'stripes'
 *   bands      stripe count, only read for 'stripes'. Wider vs narrower
 *              stripes is the main thing separating clubs that otherwise
 *              share a palette (Sunderland 6, Brentford 12).
 *
 * Where two clubs would otherwise render identically, the real kits are the
 * tie-breaker: Arsenal has white sleeves and Forest is all red; Ipswich has
 * white sleeves while Chelsea and Everton are single-colour at different
 * shades. Some Premier League kits genuinely do look alike — three clubs play
 * in red-and-white stripes — so stripe width does the work colour can't.
 */

const TEAM_KITS = {
    3:  { name: 'Arsenal',        primary: '#EF0107', secondary: '#FFFFFF', pattern: 'solid'                },
    7:  { name: 'Aston Villa',    primary: '#670E36', secondary: '#95BFE5', pattern: 'solid'                },
    91: { name: 'Bournemouth',    primary: '#D31A21', secondary: '#000000', pattern: 'stripes', bands: 8    },
    94: { name: 'Brentford',      primary: '#E30613', secondary: '#FFFFFF', pattern: 'stripes', bands: 12   },
    36: { name: 'Brighton',       primary: '#0057B8', secondary: '#FFFFFF', pattern: 'stripes', bands: 10   },
    8:  { name: 'Chelsea',        primary: '#034694', secondary: '#034694', pattern: 'solid'                },
    9:  { name: 'Coventry City',  primary: '#6ECBF5', secondary: '#1D1D3C', pattern: 'solid'                },
    31: { name: 'Crystal Palace', primary: '#1B458F', secondary: '#C4122E', pattern: 'stripes', bands: 6    },
    11: { name: 'Everton',        primary: '#20347A', secondary: '#20347A', pattern: 'solid'                },
    54: { name: 'Fulham',         primary: '#FFFFFF', secondary: '#000000', pattern: 'solid'                },
    88: { name: 'Hull City',      primary: '#F5A12D', secondary: '#000000', pattern: 'stripes', bands: 8    },
    40: { name: 'Ipswich Town',   primary: '#3A64A3', secondary: '#FFFFFF', pattern: 'solid'                },
    2:  { name: 'Leeds',          primary: '#FFFFFF', secondary: '#FFFFFF', pattern: 'solid'                },
    14: { name: 'Liverpool',      primary: '#C8102E', secondary: '#C8102E', pattern: 'solid'                },
    43: { name: 'Man City',       primary: '#6CABDD', secondary: '#FFFFFF', pattern: 'solid'                },
    1:  { name: 'Man Utd',        primary: '#DA291C', secondary: '#000000', pattern: 'solid'                },
    4:  { name: 'Newcastle',      primary: '#241F20', secondary: '#FFFFFF', pattern: 'stripes', bands: 8    },
    17: { name: "Nott'm Forest",  primary: '#E53233', secondary: '#E53233', pattern: 'solid'                },
    6:  { name: 'Spurs',          primary: '#FFFFFF', secondary: '#132257', pattern: 'solid'                },
    56: { name: 'Sunderland',     primary: '#EB172B', secondary: '#FFFFFF', pattern: 'stripes', bands: 6    }
};

const KIT_FALLBACK = { name: '', primary: '#9CA3AF', secondary: '#E5E7EB', pattern: 'solid' };

/* Keepers are drawn with long sleeves rather than a different colour. Shape
   carries further than hue at 18px, and it means the keeper still shows the
   club's own colours instead of a grey shirt that belongs to no one. */
const GK_BODY = { 3:'#2E9B57', 7:'#0F766E', 91:'#1D1D3C', 94:'#0F766E', 36:'#F5A12D',
                  8:'#2E9B57', 9:'#C4122E', 31:'#2E9B57', 11:'#F5A12D', 54:'#2E9B57',
                  88:'#1D4ED8', 40:'#1D1D3C', 2:'#2E9B57', 14:'#1D4ED8', 43:'#1D1D3C',
                  1:'#1D1D3C', 4:'#2E9B57', 17:'#1D1D3C', 6:'#2E9B57', 56:'#1D4ED8' };

const KIT_TORSO    = 'M30 12 L42 12 Q50 21 58 12 L70 12 L72 36 L72 88 L28 88 L28 36 Z';
const KIT_SLEEVE_L = 'M30 12 L28 36 L21 43 L11 27 Z';
const KIT_SLEEVE_R = 'M70 12 L72 36 L79 43 L89 27 Z';
const GK_SLEEVE_L  = 'M30 12 L28 36 L19 60 L7 53 L11 27 Z';
const GK_SLEEVE_R  = 'M70 12 L72 36 L81 60 L93 53 L89 27 Z';

let _kitUid = 0;

function isGoalkeeper(position) {
    return position === 'Goalkeeper' || position === 'GK';
}

/* Returns inline SVG markup for one shirt. Inline rather than a file per club
   so there is no extra request per player — a full pitch renders 15 of these. */
function kitSvg(teamCode, position, cls) {
    const club = TEAM_KITS[teamCode] || KIT_FALLBACK;
    const gk = isGoalkeeper(position);

    const kit = gk
        ? { name: club.name ? club.name + ' goalkeeper' : 'Goalkeeper',
            primary: GK_BODY[teamCode] || '#2E9B57',
            secondary: club.primary,
            pattern: 'solid' }
        : club;

    const sleeveL = gk ? GK_SLEEVE_L : KIT_SLEEVE_L;
    const sleeveR = gk ? GK_SLEEVE_R : KIT_SLEEVE_R;

    let body = `<path d="${KIT_TORSO}" fill="${kit.primary}"/>`;

    if (kit.pattern === 'stripes') {
        // Bands are clipped to the torso outline so they follow the shirt edge
        // instead of overrunning it.
        const n = kit.bands || 6;
        const w = 44 / n;
        let bands = '';
        for (let i = 1; i < n; i += 2) {
            bands += `<rect x="${(28 + i * w).toFixed(2)}" y="12" width="${w.toFixed(2)}" height="76" fill="${kit.secondary}"/>`;
        }
        const uid = 'kit' + (_kitUid++);
        body += `<clipPath id="${uid}"><path d="${KIT_TORSO}"/></clipPath>`
              + `<g clip-path="url(#${uid})">${bands}</g>`;
    }

    const sleeves = `<path d="${sleeveL}" fill="${kit.secondary}"/>`
                  + `<path d="${sleeveR}" fill="${kit.secondary}"/>`;

    // Outline everything: without it, white kits (Fulham, Leeds, Spurs)
    // disappear against a light background and vanish entirely on the pitch.
    const outline = `<g fill="none" stroke="rgba(0,0,0,0.45)" stroke-width="2.5" stroke-linejoin="round">`
                  + `<path d="${KIT_TORSO}"/><path d="${sleeveL}"/><path d="${sleeveR}"/></g>`;

    return `<svg class="${cls || ''}" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" `
         + `role="img" aria-label="${kit.name}">${body}${sleeves}${outline}</svg>`;
}

/* Same signature as the old shirtImg(), so existing call sites are unchanged. */
function shirtImg(teamCode, position, cls) {
    if (teamCode == null) return '';
    return kitSvg(teamCode, position, cls);
}
