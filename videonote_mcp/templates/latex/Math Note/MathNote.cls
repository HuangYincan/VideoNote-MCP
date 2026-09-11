% 改编自 Gua927/Latex_Template 的 Math Note 模板（修改版）
%   原仓库：https://github.com/Gua927/Latex_Template（Math Note/ 目录）
%   修改内容：删除中国人民大学统计学院校徽（logo-RUC.png）整页背景水印；
%   其余内容与上游一致。原许可：LPPL-1.3c，见 templates/latex/ 下 NOTICE。
\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{MathNote}[2024/11/27 Maths Notes Template]
\LoadClass[a4paper]{article} 
\usepackage[hmargin=1in,vmargin=1in]{geometry}
\usepackage{amsmath, amsthm, amssymb, bm, color, framed, graphicx, hyperref, mathrsfs,extarrows}
\usepackage{tikz}
\usetikzlibrary{matrix, arrows}

\usepackage{titlesec}

\titleformat{\section}
  {\normalfont\Large\bfseries} % 格式设置
  {\thesection} % 标题标号
  {1em} % 标题与标号之间的间距
  {} % 标题前的代码


% 加载必要的宏包
\RequirePackage{tcolorbox, varwidth}


% 加载 tcolorbox 的额外库
\tcbuselibrary{breakable}
\tcbuselibrary{theorems}
\tcbuselibrary{skins}

% 定义颜色
\definecolor{TheoremColor}{RGB}{34,139,34} 
\definecolor{DefColor}{RGB}{45, 52, 151}
\definecolor{ExampleColor}{RGB}{226,135,67} 
\definecolor{ProofColor}{RGB}{34,139,34} 
\definecolor{shadecolor}{RGB}{241, 241, 255} %可以选择自己喜欢的颜色
\colorlet{shadetransparent}{shadecolor!5} % 调整shade的透明度

% 定义命题和推论环境，并使其编号与章节同步
\newtheorem{proposition}{\textbf{Proposition}}[section]
\newtheorem{corollary}{\textbf{Corollary}}[section]
\newtheorem{lemma}{\textbf{Lemma}}[section]
\newenvironment{note}{\begin{shaded}\par\noindent\textbf{Note. }}{\end{shaded}\par}



% 定义标题样式
\newtcbtheorem[number within = section]{theorem}{Theorem}%
{enhanced,frame empty,interior empty,colframe=TheoremColor!50!white,
	coltitle=TheoremColor!50!black,fonttitle=\bfseries,colbacktitle=TheoremColor!15!white,
	borderline={0.5mm}{0mm}{TheoremColor!15!white},
	borderline={0.5mm}{0mm}{TheoremColor!50!white,dashed},
	attach boxed title to top left={yshift=-2mm,xshift=2mm},
	boxed title style={boxrule=0.4pt},varwidth boxed title}{theo}

\newtcbtheorem[number within = section]{definition}{Definition}%
{enhanced,frame empty,interior empty,colframe=DefColor!50!white,
	coltitle=DefColor!50!black,fonttitle=\bfseries,colbacktitle=DefColor!15!white,
	borderline={0.5mm}{0mm}{DefColor!15!white},
	borderline={0.5mm}{0mm}{DefColor!50!white,dashed},
	attach boxed title to top left={yshift=-2mm,xshift=2mm},
	boxed title style={boxrule=0.4pt}
     ,varwidth boxed title}{defo}

\newtcbtheorem[number within = section]{example}{Example}%
{enhanced,frame empty,interior empty,colframe=ExampleColor!50!white,
	coltitle=ExampleColor!50!black,fonttitle=\bfseries,colbacktitle=ExampleColor!15!white,
	borderline={0.5mm}{0mm}{ExampleColor!15!white},
	borderline={0.5mm}{0mm}{ExampleColor!50!white,dashed},
	attach boxed title to top left={yshift=-2mm,xshift=2mm},
	boxed title style={boxrule=0.4pt},varwidth boxed title}{defo}

\tcolorboxenvironment{proof}{%
	blanker,breakable,left=5mm,
	before skip=10pt,after skip=10pt,
	borderline west={1mm}{0pt}{ProofColor!50!white}}
