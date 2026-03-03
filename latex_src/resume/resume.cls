\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{resume}[2015/05/25 An elegant Résumé class]
\LoadClass[10pt]{article}
% disable indent globally
\setlength{\parindent}{0pt}
% some general improvements, defines the XeTeX logo
\RequirePackage{xltxtra}
% use xifthen
\RequirePackage{xifthen}
% use progress bar for skill
\RequirePackage{progressbar}
% use hyperlink for email and url
\RequirePackage{hyperref}
\RequirePackage{url}
\urlstyle{tt}
% use fontawesome icons with explicit font path
\RequirePackage{fontspec}
\newfontfamily{\FAFont}[Path=fonts/]{FontAwesome.otf}
\newcommand{\faEnvelope}{{\FAFont\symbol{"F0E0}}}
\newcommand{\faPhone}{{\FAFont\symbol{"F095}}}
\newcommand{\faGithub}{{\FAFont\symbol{"F09B}}}
\newcommand{\faLinkedin}{{\FAFont\symbol{"F0E1}}}
\newcommand{\faLink}{{\FAFont\symbol{"F0C1}}}
% use xcolor for customizing color
\RequirePackage{xcolor}
% Main document font
\setmainfont[
  Path = fonts/Main/ ,
  Extension = .otf ,
  UprightFont = *-regular ,
  BoldFont = *-bold ,
  ItalicFont = *-italic ,
  BoldItalicFont = *-bolditalic ,
  SmallCapsFont = Fontin-SmallCaps
]{texgyretermes}

\RequirePackage[
	a4paper,
	left=0.65in,
	right=0.65in,
	top=0.5in,
	bottom=0.5in,
	nohead
]{geometry}

\RequirePackage{titlesec}
\RequirePackage{enumitem}
\setlist{noitemsep} % removes spacing from items but leaves space around the whole list
%\setlist{nosep} % removes all vertical spacing within and around thelist
\setlist[itemize]{topsep=0.2em, leftmargin=1.5pc, itemsep=0.2em} % 增加列表项间间距
\setlist[enumerate]{topsep=0.2em, leftmargin=1.5pc, itemsep=0.2em}
\RequirePackage[super]{nth}

\titleformat{\section}         % Customise the \section command 
  {\fontsize{13}{14.5}\selectfont\scshape\raggedright} % 将字体大小设置为 14pt，行间距为 16pt
                               % small capitals (\scshape) and left aligned (\raggedright)
  {}{0em}                      % Can be used to give a prefix to all sections, like 'Section ...'
  {}                           % Can be used to insert code before the heading
  [\titlerule]                 % Inserts a horizontal line after the heading
\titlespacing*{\section}{0cm}{*1.5}{*1.3}


\titleformat{\subsection}
  {\fontsize{12}{16}\raggedright}
  {}{0em}
  {}
\titlespacing*{\subsection}{0cm}{*1.2}{*0.5}

\newcommand{\datedsection}[2]{%
  \section[#1]{#1 \hfill #2}%
}
\newcommand{\datedsubsection}[2]{%
  \subsection[#1]{#1 \hfill #2}%
}
\newcommand{\datedline}[2]{%
  {\par #1 \hfill #2 \par}%
}

\newcommand{\name}[1]{
  \centerline{\Huge\scshape{#1}}
  \vspace{1.25ex}
}

% Command for the fillable text field
\newcommand{\fillablequote}{%
  \centerline{%
    \TextField[
      name=hrquote,
      width=0.8\textwidth, % Box width
      height=1.5em,         % Box height
      charsize=12pt,        % Font size inside the box
      bordercolor={0.8 0.8 0.8}, % Light gray border
      backgroundcolor={0.98 0.98 0.98} % Very light gray background
    ]{}%
  }%
  \vspace{1.0ex}%
}


% defines one's name
% usage: \name{<name>}
\newcommand{\nameTest}[1]{
  {\large\scshape{#1}}
}

% defines one's email
% usage: \email{<email>}
\newcommand{\email}[1]{\faEnvelope\ \href{mailto:#1}{#1}}
% defines one's phone
% usage: \phone{<phone>}
\newcommand{\phone}[1]{\faPhone\ {#1}}
% defines one's linkedin
% usage: \linkedin{<linkedin>}
\newcommand{\linkedin}[2][]{\faLinkedin\ 
  \ifthenelse{\isempty{#1}}%
    {\href{#2}{#2}}
    {\href{#2}{#1}}}
% defines one's GitHub
% usage: \github{<github>}
\newcommand{\github}[2][]{\faGithub\ 
  \ifthenelse{\isempty{#1}}%
    {\href{#2}{#2}}
    {\href{#2}{#1}}}
% defines one's homepage
% usage: \homepage{<homepage>}
\newcommand{\homepage}[2][]{\faLink\ 
  \ifthenelse{\isempty{#1}}%
    {\href{#2}{#2}}
    {\href{#2}{#1}}}

% defines progress bar with text
% usage: \pbar[3][width]{Text}{percentage}
\newcommand{\pbar}[3][10em]{{#2}\ 
  \progressbar[heightr=1, width=#1, borderwidth=0pt, roundnessr=0.45, filledcolor=green!40, subdivisions=1]{#3}
}

\newcommand{\basicInfo}[1]{
  \centerline{\sffamily\large{#1}}
  \vspace{1.5ex}
}

\newcommand{\role}[2]{
  {\par \textit{#1} ~ #2 \par}
  \vspace{0.85ex}
}