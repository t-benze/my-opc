from __future__ import annotations

import base64
import gzip
import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

from runtime.infrastructure.database import Database
from runtime.infrastructure.remote_job_schema import (
    COMPLETE_STAGE,
    IDENTITY_MIGRATION_NAME,
    IDENTITY_STAGES,
    IDENTITY_TEMP_PARENT,
    INDEX_SQL,
    JOB_COLUMNS,
    MIGRATION_NAME,
    STAGES,
    S2_REMOTE_RUNNERS_SQL,
    TABLE_SQL,
)
from tests.infrastructure.test_jobs_migration import _seed_legacy_scripts_db


REMOTE_TABLES = set(TABLE_SQL)
REMOTE_INDEXES = set(INDEX_SQL)
S2_BASELINE_COMMIT = "1be72fe71a779eb3393b9c10dcfeae8a487d3f78"
SCRIPT_REQUEST_V0_COMMIT = "da539c3a219ebe547fe8a1b2b5ba0390c3e8889f"
SCRIPT_REQUEST_V1_COMMIT = "4b73416a6f723845fe0cb790bd7246b80f27d2db"

# Repository-carried SQL dumps produced once from the exact immutable commits
# above.  Their decoded SHA-256 values pin provenance while making mandatory
# compatibility fixtures work in ordinary shallow clones where those commits
# are intentionally unreachable.
S2_BASELINE_DUMP_SHA256 = "5eac18f2750b407d41516960d7b9c4963e1bf472fb664c63598cdca2d70b792b"
S2_BASELINE_DUMP_B85 = "ABzY8000000{`tjX>;7hmEZL%V1JomcBF`^IORm4t7vI#b0d*ilG-@?1%&~6I0!KSjDv@3fBe4J2ha`l1#l=isb%>fXZpRq-rcWXf4~0m>^!)>JUzcUeRq9!asKwjyUX?I^*Xpd{r<x`h}tA=!mPLnmM?<APnra0=hy2W*O$THF3&!mUj8Ha>-rzT>8I<9vva`maeaQh8bLKteH#Mk_4?=Q;QRvq{q*6(xLI^V^2XUHZqg!mwJM{>tcX~Tn(?H2Qa91QRM9Byjz+7}?20OF9z(i5`nBnQW~@>i<w=@E4H+#}aJl|reYrk=x4x3b(Y3FaX>v5iNA6%b!{)Ih@P~P#;^*B@>vw+*mJ-z2xwawxEEoM~<1B*pOctx*XFx-Bvsf9C1PgK(Wo?8T8dfAOs$__!$m<qy9~~L@IQ(bL=|Quk6#+C!7&X=<Fz<Cgl(NXu_%STgyw>dfc<<zm_t?JR;yif2{;-BIT&}OKFVEg-rc;rOM71w{@DXz$|6Y*8?%1YG>ZpgC)fXv?c-Qew*CQp%<*40v!D}5!RK;DIkjg{YkOvaCO;LrnY3^(+(b1ErsCJ1B`DaK*nqg5cyCsiW&~6DoouB>Jr?rNhiUK&D%Vs0EMtqWJZyvE3ZXe|!;;WF!`RQ;##5Jc%FSs~r>nIDEM3bjD1ujW%KvztGt4Q5uN{4#k#Mx%N|1RCe;O<C;t1%f)o{%40RqV@V#yA<=^l>t{$>ZD?3CT3W^n)7@(+_SuOcz|8M`gV$#xwMVt4ITsiJSlepsokp{(k+#>8B6Z!GfU=i#&T=*fhQoFIb671<Ptt@1mr*hhIQwl5Lu&4bkdm31`AAHv3pvGf?!N167gIf#yX{U|=04-LSSKj|pecni&o@4o%X!Eb6ow&;@I58>tL<cE#p;ajD?58&?JFL*A0Gj`lEKkkc?TQpZsSN(6q*VG*LT1b&V-8GgVGDNtvms)|ZG`7GU%_%Y4^YXfLZ+M?SNuJo*1GQ>%DLI8HdVpg5amJ?qxOKq^y4!dpw>$PR#0q3jL#U_#bj$}pYuiZT~yKGO|wE8^PK&$!ZX#F`Fe~z}Fqv_{pc|R4ZZt5I$Jx5i$=99sM$h5I>XWEw+2F%Z)DbEYx6gEY;En3hH732A#9MftGRUTQT67>X#T~G9)h_{xc&uVd(7FpCx5kwP&&>R6YNBH!f^CX|*sbLA1lQB;mS@o<JDSlbaD)kOpAt6s1Kg006ld#2Pvf>_9+ZOz2FqQxqqh&>lUF@T5TU5v$@OMc+O7njbW{TakQHAWq!iU=^&Du(D9D@B-0^O3S25<*=U<25Q_X!5Dg!6C)uo3BV8NhyamZ!)Xhl%u(%WWM|Q^XbDv4xXtF{IBEWGgEIdZ?{KO|<RMK{oPG5~PT*>r`DFz9yztz5ilr^4*DcC3T^CDV`@x2R<c6yW$U!YFW04;#>H10|KsYs;JbGG(c#hczsX&>l0*N;EpgWKvITauvb(h0+A=k_EEOblEW`8^8bh&j8QcXz=hk)$f7k=>W7*QaEZ*aV(f4<jZ+0tKPQ{^7PX_Hu%8VaExy+Cbdxq2arKacYKzcZ3>)&$_?g>vfBIl+%k(wdymD8;qPo2k5a%h<>FAil+i&or*A0vY6nY*G1>d0$6`qqz{n<$2ijMkLg1+o561R0z?8Evt&A@Za(wlUXX3QLiu}!hWXBgtFS4S9D-Zgo&2FshS`}9aYmH*^E#Q(`r(;;(eq+^S*BGSmVhmWJW-7?LSllu&uY3LO!+o@o?Gyxj&aHS1qkH6JpBov&#n@*lg9Z#W&ItHc_=f*m21rNJ#LRENC-7HqY0+|Z`Ps2TWPoDus)eUJ(Mkk@0rl)RkNX?os^C8R7AO~}w<d1rvUfE91z27|ggydTgPar;f$uof}{6xkVB?Tpgz_1l^zJMJ34HjIaf|q*7KZr6K2B3d{vz1ojR~SiUa9LCl<!2@E$&0l!Oj^X`43poJCW6sM-U$ZI2&xir)gx5TIHyq*STCdk1)2Wp9T+-*8hVoYK|vAb(VqB)pfwR|^Ae)fH8SKn2{vd%X&3QRIktmsrIdLe-4ZHWDim#6i8eTto<yKyjX9H6=`>&|cTv6bl3Q^M;|@%EyNgS_*2n~mAccDXhu!mz95@EVcV)CKCjr}(;Pm``xBcjma!g0)mX4sl5@1IY%(=1!rDT@M>}xgk!0P=B_{0@0_23rJvSFRIH=f7EeM`TS0ND_9*?+hg-kV$Q9&75#fGJ<M6|s<;AcPSJMi~6}QZS3RL4G)M{3+tF1ji@uGA2ec;xMAY^E8P=MOBk-fjD@Uk&EV-9!kPEig&~zrz^506~-fL&|G=C<)L7G<ERFwL2YPm%<T+ndq_u1hS00j8e=!qC=qlx!bJ51{S?(gns>kIu;}y!ZwDfDd?IL_$rhaBG{325glk>APrZBQoSR8m9QsL(nf*<TeOQ5A3%jE?;TTLx9r)(aKjWontC(m~)C`KBH6NvknX-Tp?WRh}c6bmm$?WnKGH#7Aknb~uybnlkZFz$vq>ih!RL<uN%(O5DrPxV@9U6EnPogT}VISx($~s<D@SGSLYt<&W-?>WOTGEyq`jR$>86`OQG3O*-)DNVsMmmJ+51j<2E||wzw`e&WiMb^Vy^HV|@*h<r!qrlf*)VST!^P$L?8kGezn9~+IP%h=R@SGUKzO@xCiTuRDHv|gixSLj@{tcNugWJl-V|jTI}d0%7do+RisTV=d3Bo<_s*a;D&E$3a9R?o78m+F(gN8BHmh5Qr-Y8B<PE<dv)IoS-G?Iwxr){bk<t%6sdDx~jXyoHpgb;vbJxHCOxg^VYYUErqwzh6+~OWhtg10=6K_J?$@&yazfBqsHcxC{&!xAz-2+KKnyif~%9$1o%c$8ITIJ+E%t%z_Xz77J+vd$A=UkRcGr)P~R>%S%LK0bn9)=~xAi<Lvj<Zouo-2s}?F_-QVQ`Rn0WJ(Ulct#1mZbHr6%mcli(|&7+QiV6TaoXgW*WfAzHr`vDM3@${s80LIKu{ucAd4LZ#Qx{Ogz_b=EAz^|135KzG0)g9pmLv4Fa3qWI}cE^aHtG^9DZo(_9eIVA+&z(!3#6iD^>RB)IrK33qAU1b)NoP8XRU9n6YT;>k=}H)Pc76eRoTA*?`(rhD`&fv}TWL86CX*>OPrXdF7b{pQUd-hBJ*>(@q2<e&y0-9#SJMomo&9P^OvcaJSgT1JB5zV_^(ZW36ha0iOUi)0&6Ox2X;h@Glhf9w;Q7o!b!%@#YD0D(LM;<?D~Na8i5&g$`*0j4h2JMRk@j3cKv-#q#?$0X8lOmYXyN?h^#o=BmXRVK_xu*^xuQUrj_^Zw@O3I>JVL%0EpC?nDt;NS}>*Jjmu-@HlGG=yKB!|#Xc3XC;eCY{RC1@EnjiV93-@J*F*SyVS+zrD`jS9d{!9e&rANc#*QxWqgkJe(@3P+K>^n8?HrwI;){5Ds44lyAlOoR@^Y6$=XGI?JKFXe2LG5%gB{9n07D$lU3cm&QIlj|zR8kMM`}E_(CLA18L>3FYrfNS^jmLt={0jh`6~Q)DzFdebAZ@)GqUNYl{3zD5j?{(_E5%ywZAWi1z3Q!b=<Hbt3En|5}GxbS10YFQPyT!27a)6&g&vSY=)H|gq^`+6}Shv)GnX!^KN40-201wTC|OU0#__-Z&KI{|S`1D=o2HtMQ6yjg^^Nu;7mP&AsdZpGK%odm}Ux2Vcgi<&n93Cg4Q*6hLiqPnfiC?-~%eJfA2T!}l9iwCWSlO0<E+P>HUqAvSYI$8vak5n{WY7r&-v|)E=ajmFfkF`-^l4a}<teKL>DgO(cvVBU=K$Ow3D<4e(Hjh&wvIJnu+VKI8+KmW+wU9y##$|yi<opVeCY;3P8}g)jeb|n-i9j5eh9+gZ$<jEKLwjr{>K817t=N|tVGEDfnAi?b6LoZ^TUIH?{DkQ`>{e`vVkPRnhd$Y6Yv<>o<$Pk(Je<aOekFKYnUAdBSUW|jVSr_*7-f)-?G-34qu7^Lc$v#ry%1TAg~dwC>#D3Y4I%76aWJlabl442xZFkba#l>ziE?=0DQLHkV%N$K_6;Roux)UbK(YP$z@~vrVevC6OW?m{0Y*Rjb&FKMa|Q?GJy-k-m8l!7_>SLMYeKNavWRAaF1QnmzoBtO5|s%t3`6f8!tXrB5=UhzibkPeWWcv6|Fne<t0qc^6N8R688NXf+eZV>T8<ZFr+WCEh$3YvFgiv_G>$(HE3hUkRG@E#p#(>(KH_8U$km1IdxTe#gU0Jbaah}MR&m^I5v^Pbwo?GxDx&eaU#z-coGx^%w_+RbmW(}!PR_};nBoAKqy}SInN1HaJH!%t7-YYBn@y~jiyWl&3<pIRZ0N~DoV7Hj5n8TOsf)|v)HHzLx)H`kHK{S1f`3cbc!hyu66@0U!SeO$SQVWWX=@aoCTQt<4Rkkso1rqF*NlcqNB=r3s^0#0Hc+afDdHky5>k~iK4N5St81J2$zRrr-W)O8JpA&bi=^fH4?3emtq(yj9Wz}4HbT<UN(uY|vewL}q~W4kNjxh`MOm~}zc`;sy>N=be2je*?$et}qhk8p4_V2et#dQn9iyuu59I9pYJGW)+kX*!?HI_{!GE8A__V%SE+{2~H`dRz4(m7J-7ziT!h{n1`sVeUKfZkZr<bq4xjz2p&&O~6eEjXJZ~yd%|5?~qpl7Nn+V$XC(I?r)?Sb2TZ_hwOlwd&%>Zmghm-(4tcZw)k&#UgbmdY5czRvKVLs=XQs$<p$2El0#x>>p$$j_D18i%9dHs2(n`c!Avk3lC9Wfvv23dAgW{PJ|x=FuJaZ@dtV{QIV@kpPnl2u#GY?uuLPx9v!jHM`-3PH^|47*oiN2VBR(h!HJ^qwaLzKFuv?^dQlLCkS>=ul%OY8UgY#j(<z1Gep}uI7K-@@`=@fH~`g!mkeEv%G=a=h=cnbTdV&LE(R?}5Tnu~^fa?3B-gROM2sfizoR@;gTqNFO6=NL0sYD}04~G&0|S}YkE|`F{qt$q@1a?Ui9FBGkbF9>CnBYY<u_?gU}oC%7oQr>TcCcAgG5l1^pKw)wd#w@|61h4G}2Gk?-qvQBU$<@p09Zw%d)#JX<j!~>s1opp4J*wv^bu=>J0G|dZWBT{R;J^vnJ&!fbDjl!}{>k*4lub3V^N20Fd^2dV1`W(q8=%fS;a?KFoW`6Iqaq*q2X<o)3jBz0-T>XCKU$`ubZlF5>mho)C521Hbysw)5FE#|SfBnc5>^i$|mf?aldJcttX#dDaq$qd6syZog_*`W{F_Wjw~_bmPM+y0;ZOkg9&z8W_35l~tbRopLYR3jB3)pbYY0$w_g=NNy$3ll^#|Uk>VTVfq&OdHRLo!(Q)JOYYn5Pr8y!+Zqe{(i@#xjZwoARsAhb1C})378>B<)-P4}*jymYmG;<QqBW-@BJa?{liD{4(Z4hhB}Fj2fuHwgT6(@$c1&k_I*XyGP%Ma(8`}+0FL#rp1uMsVwA2L}o35^megoPPBZDj->0oJM8M%q#M>$WxacOH}{+OsfCd85Mq)BNh?ZJ_otaxG1-{iwh{wmyVsY>Y?CKYVIiItF;7A>>Z_2~-WhM?3cVy4vYww6hlq(BVeHj8d<Na9MK7gr37y)iRQv0ODKPz^o^CHT0g8=%b755}cR(y7<92KMEBlw$TG77%0!BYYuH9w%umGNakOtvJ;hp_F0f`j2z3>qS70H|S6F)O$#m)60va{53uS>HA<6iM09kyyDGNALco(yLK#T_PBUy6Iq~WwKJA{^kHJ9shNnoSDwmV01gOv1EhJ|SEgKnE6$^uD4xOS#?rIA0thJwE8Z`3&?TUBi@>+335%^dapt4$Kyox2(zWhfF;;JTBn;R{S*=c$)lmCJ4F+PuZB^`t*EdC!KHf!Xu70$Qa_DMsdtvD22EP{jO-h6i6Z0E!G3%8)KtDOCtOV=3RFSqoMn6K|yNVpCscx3X6+92EL(G?~#%~MkNXE`hx+9djEz64L7JJko^vNS;n3Y6jjUSul-0Q?tl5P5666i=GZ7I%ord<+Ia+SN?Tge-<l9t?c*Ikd{NL&0|wyYRfMat~4BQ!5$24d}9&I1}v3ilnPjDzGE`z7*<kW&f_iweql7%$Rfd$;=;Dl(A#mzK0%D#dw;U%rey^OCwGHtl)|GZaD9n*#DS`aT+yulrUAhZZ-|pT$}9B~md6KLYZ1F1x%yANv0nbuYy#_Zw)Yv1#OE_AGl@DD7PDXilKJFF<U5L{^t1x+Tt{B0h&c<3cnVN=*``)bVX2i_hpKCF)18BKM)k+qE^43p3}e!fDT*EU7)q)cW6(JCGDBk<;U}jP&B`8MGyAnBR*Ei64Kno}KE18ub`~L9L{t<T>`$43p|9o5d`vO+^_jBxPFDAn0e=w*Ez4cf660%rlrooVI+N70iS$z<R;!5mm7v#s|XaO>*iz(MOJI%TC*#^U<O*yrqKUvYSIihB7R~YX+Ig3CCH5$}111^HVmQ5Yr-W3Zob9A0lwm_evpvBlB5KzxQ8&CA61&Y(}~@cI8jTmK!yJdZk9Zyv6U()V2O0D8CIBud+{j`C3`Igk(VA)gv#U`gt@!e47{d8A&i)0r-pdW;a<RcWM1|5pZWmOBcGAG3Xu7sm-Z{AdSr2QZo$$_jlJ4t)`6H)F_z_DI-23!;475mkC}?4n}dk<sXbf07?p`vsT@cN1!BS7!QB2Q(JyZMQMu0Ch@TpkH|CC+XMWjkxTdT`y0TZhUG5}?=CJEAPYzx^CuCp$WvLR>`5d%0l`~}ymnr+DJuQx6#ZEUpRkH|DZA(`S;#c?{f@tvTvctUe@2CcR_Or`tN3!@P~K<ZGny@uJWF(A>~MPo%Y4<)zsp(o!FP225nj8&Yc~^Pg4W;?W3IVHKAAMtl*g>@BWey&t!qBVQrq=sOJkyIs~qnzTdKq93d12-9kw9)rsXdlph9Vx=2l09ha*C;HR!o8$uZm7GS1_{c^n=lQlUPi^>Mu!sqr{aGWbrZtvsY(yc!hlinjV~mC((K#wUsKprv@fRP|g&FLSbTld8bm{&Vtq{W6M`W;1#@0N4E%3hu5bL7Ei7+4=kR&%i4Wp|(C;s8m@Sb$oHIFDtjy7H5;#_wwvV+y{N1c@Z-2W7OaGYt(+hr@y^NET-NyU_XMjeYO6ues>*Qo}OK;m#5!fT!OG!a0G!`6)&ntfE6n8<RyJT`si)2K7a2&d~lwLdHDR*%`Z0me$QF`ah6IGYF8Wv$!nP#$~l{{GLd4z$Ql?iNTvGO2S!wCUH`r!yANi^C<my`L+*8vV>$bV6P8rMZXU3N**wnf;twCD8C};>8K)0s)+XdvbL_ZT+m*P-qeE;e3QpyU4!gOE_(&4<y>Me#B#{CdVMd#$EWx1m9RP8KYz9;gS=fH)rlV;rZAp)wM_Na{h=W1#cTIk=bR$~JJ|<p!H_t+I;4MCL_aj3YjzKM5kDXEejiuu;jO0e?x*BAD8_90i&`oV}A`1%}TE;E4T@Is-zFoN1M()GDC}ZT@Nc6<cnJW1EPvEr*^Yf%PHO+nM;?wS#>7cx(s2(IFEpc`-JcnZezt!Nd$IZ7YHt7^-`Uun`q;bL)+l9eFlk{Mt@A8Oq7SXf!NV_U7(lL=`fhzKV??>fM{NrphndI<+LKZd9O%OYwt^9kT&5umTyyUMrOHRaowfG<K^x84lagL{Dzz^&Q#m-e4jE2zUpbJ_ggv&?x=K*ojGKB`2$%=|Fy8@ec?Jf4m9SOzdEUgWqza+L4FeaQvJ#9ir0W8$4JWCgf+uXVy)W<0Md*Ae(BF%7znEOZ1e6aVe1<xqYrrE_qT;Fx8KSbU?+cjS6@#=JlXc-bGs+IC|rgl~EDjaLhbEjRaF@6iaAGr`G%A4Z2HuAb?W(8yDGPmgk%r;Md(Q6map{%5<o~z*9#p#Fj)w}gle!LxA#>J7}kd?6#^Ipp1@!+7i7J!V3hUkW`VN0{q*}Mb9%mem7+FvBqPuMbF(;PK>4D0U8&dNi^9;~go0Z!#96-2t*;TB_5hpXEiEi!%|w+~VIqNT)WL@U#;xRvZZd8&8J(;w>p{@_R_$VQuKoDAn%W5+{2q?6<CT#vXE$y{ud?$)!xJ*h)>QAc<oVDKpycg%48U6&qqeE-D^Z@$CQp))oSJ7LD*V}as(Ys1q7*%8;CHHfbD2N83+rO_gDzx^wLx{kY34ksz{;!{PAC27GJ>8hi>P$Y}7E}^}AmLy?c44ngJFaPDcpsOiHoA?b=R2l<?gO{MBAu4t8c<pLS-(9uznVqvqoBHo-gH0cT(l=Jqgx-tdwk@U3bTwm_x~g@K%9PkZSEtfkZ*^rXkh;<oA??csN#u$l?UUo+^!$BSO!VYfw+6^0tkMA51iUkz;sQoLk~+8AbV|_(xkSiSS1{l`h|Tedg1KLAm($`qq-spTd0qn1HV}P)UYj?f4cd2z`5VjP##pd{|NNfbUEQbk9!26pf47Ql%K7^5uQv3i8GXH6=eN_{!avqOpIu#FX^u$;BRvj&xV-pio_kOkF|a{ZuhQhiG%`v%uW*L63H}0GI7Fm7YBBHYgwvvlJ34Lm*s)8x-?y|#x#`TvIuYM<qi3E-9OZe@1RD~t0VU=Wq|r!d7MKV(BCFGaZ(fWpfpT7F#o|0UUGDiQL?PjM(sD|8W|GEWMzg8oIc7AAMx=8$(|NM4@lt<^?9oHWbF63ew36!++XztVbk;WY@bk7udehvwE!NRv?TiLL>u7Zf|5&C0y5T&y`0)N!oJHxLUKRQ3getJ^1C3s}xTG-318*Em{>%(Xbt#8vbSKDNOK}p<++Gf%eCURivONX}`ftow;s|W2tbY+ptdIJ!Cp<4Rs>3m`@Jx7EO&Zyp=n-|_nXz!m)6b*(zzHJ^JQdJnF%F^CnJXV!V8ID1<yG%KaKa2nt9<H&9*kE0&_Zum0Z*nf{<jNaFjVD37pyu^`7<+QokwN8D~7WieCz>_FBh})M35GBJwi12-V0+A#a4{U_hyXjnJ!+yiQVOt_+|X9h41=wFTu9mCx`PmIvE`ZSs$deVwy}e$NP8dIj@!AUp>Xy(jt701pvJLBKSF6PjRGVC2a)$&x^BjoAX6u3Btqb9jl4>6w8#ZzChW3W5dd00s^StG4A5U%*CrrI#>KiZ6l_u@smiJ*a=S7fgyTD-Gt%z^?t{&^G+>6Pc{{$h2paF<lX7j^|E>;<m>4b%U1S9!{lH;0Ha)_iJOOD&spx#1jd}egv_%*R8yTK<J6vDfIrvNW_e2lAB5%ZUx(K2$w(kenJsPJi`>mD!jKTmTQdoPZ|bda_|rH4a}Hm(%~6}+n{R#_OdES|EkiwPQz_`Y^`M|=R!rK*ZmiRH@R`S|<<0z#WJM{$493hGNyZdlY2+>GK?lKxK`M?$;EgcMly)OLGesT86LffHg&)SVdSV#DJ-t30A)79px7>gB|3Qi0Mo{8kE1Up6V_PdEB|b32Fn3b`u)n^zvp8h%gE@_3p3l-Pi67&Pgg0$eCC|a|=)URwwwEu(iy1G!>q^x9?_FACxTb#&$OC+xJ%V<K$y>RGoe3K(4~X-9NfH4YAZ)OthjP#fnhfOVmHyT`4ONKrR5q{$JITWsg;f5A4`i`gh_M>{R|-uDN2<TaGrl^^!|(VR;*n{RdGWF+VO~MABLUqtm~LET?H;dMkXJXaq$(0rvj+FDVWc+98)#dZdN<n_HaAK$^1(#M6F7QK9_c6<si49-v?+U=aJKI@nLB>y2NCk*bLl8!Wu;)>kUn%6tWV$l6kJ~X{i(~D<}_dw=-jA&Iq99tl)ZQDh|-wDGeEaoR3aO4(;a!7*Nwr5NcXI}WZDN+ac@I<zrzZg@`IIv@c}PZ(?urP0{$I*RFlSrOU5#g7+@wAL6pnps<`e%Y$&CPz|saezV?8_A-@tjVJ<;yeB=_%n0^D^T9;B{JSy|jETcl>l@W@v_<%f7&hcGYbfQXm{PSvDzDO$xG>E%BUbw-Bed@|4`A8r)kccCi!iBO^Ni3115&09<sAXc*8q*q$jVSAIGF+J>b7bq{(t`*Y>xmT`YFD}IZB_hAaw`Rzxiz>?n;m7ltJ9Bbj?0(r_AOX_<RPt_w__q#6uS+hzs|RD#uNh9F_y3rCX@ew3+?a*>tZ_Vc=6o768j96aW?xL+TNlxrzaW>B|b0p%(3ET1;jO}HN(RycP5~mP}7?_u2544Br>hExqjoLrl+Rn4HEOb^6~uVQdT4(Sg^3D^i%0=F)@>O7oX0rm%lqQOh`GNdNG(rG#KtlU#k;mL+P0w{d{$Dj2;C$S0~^fwRNZ*Z<&P+2O|$9eN{Z{;8{Kz0-^u)nsf3I^<18HmfxwfG#*4cNX5{n)GenG!-}!vHf3lp><4m&HGH9$01S@9|HrWfw^-zlr#~;T`W*Y%cI4)<B#7vRQ$x`1)a~JPFqL&!l;`)m6rYQF{0(qVAA0)gWU&D^03T5)7hj0VRnqRqHX527K#-g9&Ux^}cyd3RX^b-&pJQ9j>j(D0a1!7a@o9i#mSvkLzFlaRhc1yTKjBF;J<M$N;TJA(B^1^P$ekT{E*F>Y!Cv_OA41a5?Bn3W*~hbC%`@{%e7E6+znJQjJiEvFVqUU`xNzEkW)OGua^kS9Rq)G+4P!_yw9UeGsyKhDFgJ;EJ;c$3_3pFi4R?!&D~vi<vt*9!9)5|DQiFFw5bh2K=@VbXFkI;`5->kK`HKKGou(X_1YM^vWn+C@=3!WQMS50Ix^_9LyCT27`1S=q0;Lr5b^S{Q9uZbiXfZeMYv}yqBk=ay7ykzh9NNiX+W-I"
SCRIPT_REQUEST_HISTORY_DUMP_SHA256 = "9d443a07330b847efc4e0c77591f4cf9a22a7b22feec62607d5f65e52253eeb1"
SCRIPT_REQUEST_HISTORY_DUMP_B85 = "ABzY8000000{_ifTW{Mo6n^io5b|ONa2F><x3$F{+*av~CidniTf1QpXo<Et%c4qBxnS6T-;qS!ERv#=bX(9GzMKnrF5kK6t@~j*bG*f5b~SnDP3N;W&)zNEiRU`r<n4v)pf!$#k7GuoXa{e(GkoUk{l#d99nbybIX^9?my^Y3=cD_1)FBHo4;V=Vp>aVyo5TO>i;FHPzvlw){Dje6BKn9KPAG2^Iq%)`$@PWj4F39i(B;5)I7kI$I;J2(X^0h!D2nK<&(a8M75@H*f{zO%QY}k@<B-H_1z*505;*ixuMyHDR8qDGkN2IMhD7)gT`QbQLT5Vj+z;-e=~ZWP?aik%kaFqHyzVgs;#(hBdrq52RWDIs6DkS1k0>fMD$YZ87>vJ54zLP@DT+|0+mERrD-z@%RFS{Jgl|$Gba1#*b#Y@FP`37Yl&<S<dTv<S)S4mKAaNK&9cFe6_%0y~b9;sW?3eDED%Ad}=$7|Jx-E&r_BWc`s+~D7Ivt=U055`Oe3Ky_303|+*mp8ON}2r(^*KsQ^)S#>1QhVs`bgkw4r#T=(4}i;{jj|FX#YmXMyv9GG8`bLH0Hw)Mgbkp^=$g{wW}~DW`u@5008Sn8%YaL+>c8EP6Ubr>@!Fk2QGYJwD%3+8JO4*W5y`+1De7eOXC=jfnfGk3PVw-eTKiJ0G}G?b^iLXuM1_4RjKgv7Q-4aiHNY{9HNEg5P_lt@<`f9;5%W<LSlhg!Z5sAL-TFuZ-B2qUuBPB1B&{F1K&YI5YVt`2N{PdjUk|qzriT%JzEHoZAqEQOpw^v$+Vyh)55*}j@w)cz@k+Pbz)Z}1}JQkswp!Fz_$lcR`pL$`6mvt6m6inrlvv&L9wRM4GzsRvu-J~KF3gpL*6u0H;P`2$2OgWV}_mS?8;qunU=Y8v<Ev!&M%XTYxin6xLQ1aH9j4T2HxcAqkJ3U8$SmJc)uYW+IeWyBKQC$6pB-Z&-n*Po|V%gSR%d|jPiUNK<!K-NXT$78jl8WdH5Z|3SLhbfG`;31Zp2Y;V7d!DNe{wiUWO9(A!YvGEcu74bH}A-##C|e110e#^cxdzuyL`uTPs_pT6?WUc7$!N>Z<&KtS}%OXM$^JNC)H&&fOB4*f-z4T5fQ?5A1h`sDq;C9c~6tvql4SW(S&y)t(Iagiw%NSr_-<R;=4PzZsbpx2TIDA?dEcf%%24|$$jmE*Xcyubti_||!}<aq=@cXI3h$>qHDm`Su~#+;!Hg;C+@QNxYqo>8D^tLZrU8;nM`&=*xT&i6$Xq*_IH=xk4+)~Xf)>28PEy$*+ILIOVm%rdzo#<zZi5z9QyH9cc>kg=M7yUg2qZqg$diX}4V#Wthr@gR5}Doz>+{=eKT*dvPBxsBqXLj<#<LmU*dfhqH7ydoja#*KWO(kF5i47!$q@GbXO5n8Wt*w-tSJ%CnQQ9fE>r^p=+g)s+?fO3&fKMJ2QK~@NwnUJsp;ioqaAB-dqUD+WBt#zCowTeWA?J(MDHx}`l#2BVgrT8Tyc-24R%aQYNP<Z^tQ5C^5P4pm|AeI4lGs%?=H;R0##41QTfz_fLIm?I!TR-0=<zk$e3M}7J1~r$9G8+U=_N-YO5$ZE!XB0IqC|w|Yx@up_2?+-j4YIjm#puq@%3b|ygm^wsF|ovztg2z#YtPH9>&o2Zt%a1@NelUFd_(n{%?E;XOR?Ql-#asx$1J)T%hiuy1W+zZA~4q%i(N{RvV?N{xPo99tYesIQmb^?>rBkcR83FPE^rE%uOr5sb5NlofTMEGR1=-g7w+`KtXLIRF>nlUf9@{a**o`2Mg|}Dc7Fx8+^GKtk!cq#Ji%z&zkI8p#Xz&9;k^$~whig6&cG-PS+Nxs)YL0jd8>T5Ry;U)nx_BhBOuAZJ0J;CKXd#FNCQT;x@$qVyzirHEzqm;jv6OA{ud1S@CGPdW*erbo1+j^RC4p=qtuGBTg1Y$_UXHaHeFryyRnrrXq`}^Z=7DLc3&AA_fyEygE}sABlDOcP3wYk!&duZuQ6(mmV!c)V!Bh_;Zd&0irRbC%Bm)o{ZF$y+W)QYjq2#0&fdG9AcpVq-+9q!`!aUsvtofTtg~z+<4P|<#&FGC2epbxu|UhO0j2{9%JrJOrL8o%9MegY8<iTKRdRKYvGb#LA_G+EpL-ilFF~ehehE(oqt`r@*=r_);T5H!FqP^$3gSjk)Z*p7s-vZBLCVk27#P%))J>oV4#O=P{deJAgY*XNaK9n8Rgr&XsiQ7DcEV-fYbjkuhV$!>P|`}!|Do=lRY}r*GEk;lsbueGW4i50YV4s^l2j3+7uI|Rrx2!3iG4xM=O_w6=FHFc9)2-W7nla0hwTwt&sR^bHE(yC)${8H+)9{+po|9{|MaS=D)zdoE>W+&?y)rbD;Yf}kf_!zZ%u$x7Qa%px-vdYZDdp%nba!m{ocKBLGAfse(4<XFA)(q1H?32OCG_g=9iaK@6EG+0GoRs|4t|X00"


def _identity_ddl_drift_cases() -> list[tuple[str, str, str, str]]:
    """Systematic valid-SQL mutations for every approved DDL dimension."""
    cases: list[tuple[str, str, str, str]] = []
    for table in (
        "remote_runner_schema_migrations",
        "remote_runners",
        "remote_runner_workspaces",
        "remote_job_attempts",
        "remote_phase_receipts",
        "remote_pre_run_observations",
        "remote_protocol_frames",
        "remote_runner_enrollment_challenges",
    ):
        sql = TABLE_SQL[table]
        column_lines = [
            line for line in sql.splitlines()
            if re.match(r"^          [a-z][a-z0-9_]+ (?:TEXT|INTEGER)\b", line)
        ]
        for position, line in enumerate(column_lines):
            name = line.strip().split()[0]
            # Rename all references too, so the mutant remains executable SQL.
            renamed = re.sub(rf"\b{re.escape(name)}\b", f"{name}_drift", sql)
            cases.append((f"{table}:{name}:name", "table", table, renamed))
            alternate_type = "INTEGER" if " TEXT" in line else "TEXT"
            cases.append((
                f"{table}:{name}:type", "table", table,
                sql.replace(line, re.sub(r"\b(?:TEXT|INTEGER)\b", alternate_type, line, count=1), 1),
            ))
            toggled_null = (
                line.replace(" NOT NULL", "", 1)
                if " NOT NULL" in line
                else line.replace(" TEXT", " TEXT NOT NULL", 1).replace(
                    " INTEGER", " INTEGER NOT NULL", 1
                )
            )
            cases.append((
                f"{table}:{name}:null", "table", table,
                sql.replace(line, toggled_null, 1),
            ))
            toggled_default = (
                re.sub(r" DEFAULT (?:'[^']*'|[^ ,)]+)", " DEFAULT 2", line, count=1)
                if " DEFAULT " in line else line.rstrip(",") + " DEFAULT NULL" + ("," if line.endswith(",") else "")
            )
            cases.append((
                f"{table}:{name}:default", "table", table,
                sql.replace(line, toggled_default, 1),
            ))
            if position + 1 < len(column_lines):
                following = column_lines[position + 1]
                cases.append((
                    f"{table}:{name}:order", "table", table,
                    sql.replace(f"{line}\n{following}", f"{following}\n{line}", 1),
                ))
        primary_lines = [line for line in column_lines if " PRIMARY KEY" in line]
        if primary_lines:
            primary_line = primary_lines[0]
            cases.append((
                f"{table}:primary-key", "table", table,
                sql.replace(primary_line, primary_line.replace(" PRIMARY KEY", " UNIQUE"), 1),
            ))
        elif "PRIMARY KEY(" in sql:
            cases.append((
                f"{table}:primary-key", "table", table,
                sql.replace("PRIMARY KEY(", "UNIQUE(", 1),
            ))
        for occurrence in range(sql.count("CHECK(")):
            cursor = -1
            for _ in range(occurrence + 1):
                cursor = sql.index("CHECK(", cursor + 1)
            cases.append((
                f"{table}:check:{occurrence}", "table", table,
                sql[:cursor] + "CHECK(1 AND " + sql[cursor + len("CHECK("):],
            ))
        for occurrence in range(sql.count("UNIQUE(")):
            cursor = -1
            for _ in range(occurrence + 1):
                cursor = sql.index("UNIQUE(", cursor + 1)
            cases.append((
                f"{table}:unique:{occurrence}", "table", table,
                sql[:cursor] + "UNIQUE(id, " + sql[cursor + len("UNIQUE("):],
            ))
        for occurrence in range(sql.count("REFERENCES ")):
            cursor = -1
            for _ in range(occurrence + 1):
                cursor = sql.index("REFERENCES ", cursor + 1)
            target = cursor + len("REFERENCES ")
            target_end = sql.index("(", target)
            cases.append((
                f"{table}:foreign-key:{occurrence}", "table", table,
                sql[:target] + "remote_runners_drift" + sql[target_end:],
            ))
    for name, sql in INDEX_SQL.items():
        cases.append((
            f"index:{name}:unique", "index", name,
            sql.replace("CREATE UNIQUE INDEX", "CREATE INDEX", 1)
            if "CREATE UNIQUE INDEX" in sql
            else sql.replace("CREATE INDEX", "CREATE UNIQUE INDEX", 1),
        ))
        match = re.search(r"ON\s+\w+\((.*?)\)", sql, re.S)
        assert match is not None
        columns = [part.strip() for part in match.group(1).split(",")]
        if len(columns) > 1:
            reordered = ", ".join([columns[1], columns[0], *columns[2:]])
            cases.append((
                f"index:{name}:order", "index", name,
                sql[:match.start(1)] + reordered + sql[match.end(1):],
            ))
        if " WHERE " in " ".join(sql.split()):
            cases.append((
                f"index:{name}:predicate", "index", name,
                sql.replace("WHERE ", "WHERE 1 AND ", 1),
            ))
    return cases


IDENTITY_DDL_DRIFT_CASES = _identity_ddl_drift_cases()

# Independent, reviewable fingerprints of TASK-6611 section 2.  These are not
# computed from production constants and make a production+test typo fail.
APPROVED_DDL_SHA256 = {
    "remote_runner_schema_migrations": "a187858364813e7932ea893dd1314146add2ff5e182346e6e1c16aa0fe446920",
    "remote_runners": "c963c00352242c793095b23112df81f2222b989f8bb9050bcee37ac422bfc631",
    "remote_runner_workspaces": "be68e272c89d4a266466c450377f51e3655604ebff8c92661b2b59d616831e95",
    "remote_job_attempts": "47ef06091451151f1ac3812fa3f189bb509558a0f3eb0cafde52ad1ca42f4862",
    "remote_phase_receipts": "c0866b572bb587b59c092da317d37bb695816b6a221147fbaea878096f3f2262",
    "remote_pre_run_observations": "ab1f2c64fdba8c0e3cc2fbb910e814b4a698a48445b9fb513ea3e0290469c73b",
    "remote_protocol_frames": "a8927c90de9442b21e18600c94af3d9ff1d027437334446d4b602b1ed1cdb316",
    "remote_runner_enrollment_challenges": "135b02adf682130fd8cadda6cc1053cb54b59d4743bb475e5d25a0c1fcaf3383",
    "remote_one_live_workspace": "4b5e413b33f1de2464725cf67bd3bfa3cab8854756035f23b33bf03147259be9",
    "remote_one_live_attempt_per_job": "eeaf830bf26a4f44987a953776695ffdd20f7d9a98d53bc5886148e24dcb0af3",
    "remote_one_live_attempt_per_runner": "7d197c2343bbb923614b80b03684b5d29a18bab78e09ac48a8b290ffc1dd3deb",
    "remote_reuse_lookup": "9a3ce6f4c824eed67835ca285146b7a5cac2efced30e1e5f266a4fc3c258b5ce",
    "remote_enrollment_challenge_expiry": "6023bc4fd49a0cc62c8c0651b8c4b4153c687887eef88f07f6a69dc0007234ca",
}


def _normalized_ddl(sql: str) -> str:
    """Independent normalization used by the approved digest inventory."""
    value = sql.strip().rstrip(";")
    value = re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", value, flags=re.IGNORECASE)
    return re.sub(r'[\s"`\[\]]+', "", value).lower()


def _execute_section_6_proofs(
    registry: dict[str, object],
    required_claims: dict[str, frozenset[str]],
) -> set[str]:
    """Run proofs and record them only after their shipping assertions finish."""
    executed: set[str] = set()
    for proof_id in sorted(required_claims):
        proof = registry.get(proof_id)
        assert callable(proof), f"unresolved proof: {proof_id}"
        observed = frozenset(proof())
        missing = required_claims[proof_id] - observed
        assert not missing, f"proof {proof_id} bypassed assertions: {sorted(missing)}"
        executed.add(proof_id)
    return executed


def _objects(path: Path, kind: str) -> dict[str, str | None]:
    with sqlite3.connect(path) as conn:
        return dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type=?", (kind,)
        ))


def _snapshot(path: Path) -> tuple[list[tuple], list[tuple], list[tuple]]:
    with sqlite3.connect(path) as conn:
        schema = conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        markers = conn.execute(
            "SELECT name,stage,updated_at FROM remote_runner_schema_migrations ORDER BY name"
        ).fetchall()
        jobs = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return schema, markers, jobs


def _complete_snapshot(path: Path) -> tuple[list[tuple], dict[str, list[tuple]]]:
    """Capture every persisted schema object and row without normalizing SQL."""
    with sqlite3.connect(path) as conn:
        schema = conn.execute(
            "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master "
            "ORDER BY type,name"
        ).fetchall()
        tables = [
            row[1] for row in schema
            if row[0] == "table" and not str(row[1]).startswith("sqlite_")
        ]
        rows = {
            table: conn.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in tables
        }
        return schema, rows


def _downgrade_to_exact_untouched_s2(path: Path) -> None:
    """Turn a fresh current store into the byte/exact merged-S2 boundary."""
    Database(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP INDEX remote_enrollment_challenge_expiry")
        conn.execute("DROP TABLE remote_runner_enrollment_challenges")
        conn.execute("DELETE FROM remote_runner_schema_migrations WHERE name=?", (
            IDENTITY_MIGRATION_NAME,
        ))
        conn.execute("DROP TABLE remote_runners")
        conn.execute(S2_REMOTE_RUNNERS_SQL)
        conn.commit()


def _restore_pinned_sql_dump(path: Path, encoded: str, expected_sha256: str) -> None:
    dump = gzip.decompress(base64.b85decode(encoded)).decode()
    assert hashlib.sha256(dump.encode()).hexdigest() == expected_sha256
    with sqlite3.connect(path) as conn:
        conn.executescript(dump)


def _build_exact_historical_s2(path: Path, source_root: Path) -> None:
    """Restore the exact merged-S2 dump, hermetically, with pinned provenance."""
    del source_root
    _restore_pinned_sql_dump(path, S2_BASELINE_DUMP_B85, S2_BASELINE_DUMP_SHA256)


def _build_authentic_script_request_store(
    path: Path, source_root: Path, commit: str,
) -> None:
    """Restore the authentic v0/v1 schema+row shared by both pinned commits."""
    del source_root
    assert commit in {SCRIPT_REQUEST_V0_COMMIT, SCRIPT_REQUEST_V1_COMMIT}
    _restore_pinned_sql_dump(
        path, SCRIPT_REQUEST_HISTORY_DUMP_B85, SCRIPT_REQUEST_HISTORY_DUMP_SHA256
    )


def _insert_runner(conn: sqlite3.Connection, runner: str, generation: int = 1) -> None:
    conn.execute(
        "INSERT INTO remote_runners VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            runner, "sample", runner, generation, "available", 1, 1, 1,
            "{}", "{}", f"att-{runner}", "2026-01-01T00:00:00Z",
            "2027-01-01T00:00:00Z", f"serial-{runner}-{generation}",
            f"spki-{runner}-{generation}", "2027-01-01T00:00:00Z",
            0, None, None, None,
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", None, None,
        ),
    )


def _insert_runner_s2(conn: sqlite3.Connection, runner: str) -> None:
    conn.execute(
        "INSERT INTO remote_runners VALUES (" + ",".join("?" * 23) + ")",
        (
            runner, "sample", runner, 1, "available", 1, 1, 1, "{}", "{}",
            f"att-{runner}", "2026-01-01Z", "2027-01-01Z", f"serial-{runner}",
            f"spki-{runner}", 0, None, None, None, "2026-01-01Z",
            "2026-01-01Z", None, None,
        ),
    )


def _insert_workspace(
    conn: sqlite3.Connection,
    workspace: str,
    runner: str,
    *,
    runner_generation: int = 1,
    agent: str = "dev_agent",
    generation: int = 1,
    state: str = "ready",
) -> None:
    conn.execute(
        "INSERT INTO remote_runner_workspaces VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            workspace, runner, runner_generation, agent, generation, state,
            None, f"root-{workspace}", "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z", None if state != "retired" else "2026-01-02T00:00:00Z",
        ),
    )


def _insert_job(db: Database, job_id: str) -> None:
    db.execute(
        "INSERT INTO jobs(id,task_id,agent_name,title,rationale,script_text,interpreter,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (job_id, "TASK-1", "dev_agent", "job", "why", "true", "bash", "2026-01-01T00:00:00Z"),
    )
    db._conn.commit()


def _insert_attempt(
    conn: sqlite3.Connection,
    attempt: str,
    job: str,
    runner: str,
    workspace: str,
    *,
    runner_generation: int = 1,
    workspace_generation: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO remote_job_attempts VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            attempt, job, 1, runner, runner_generation, workspace,
            workspace_generation, 1, f"bundle-{attempt}", "terminal",
            f"fence-{attempt}", 1, "2026-01-01T01:00:00Z", None, None,
            None, "completed", None, f"terminal-{attempt}",
            "2026-01-01T00:05:00Z", "2026-01-01T00:00:00Z",
            "2026-01-01T00:05:00Z",
        ),
    )


def test_fresh_database_has_exact_s2_schema_and_nullable_job_columns(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    db = Database(path)
    db.close()

    assert REMOTE_TABLES <= _objects(path, "table").keys()
    assert REMOTE_INDEXES <= _objects(path, "index").keys()
    assert "remote_runner_keys" not in _objects(path, "table")
    with sqlite3.connect(path) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, declared_type in JOB_COLUMNS:
            assert columns[name][2] == declared_type
            assert columns[name][3] == 0
            assert columns[name][4] is None
        assert conn.execute(
            "SELECT stage FROM remote_runner_schema_migrations WHERE name=?",
            (MIGRATION_NAME,),
        ).fetchone() == (COMPLETE_STAGE,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("stop_stage", STAGES[:-1])
def test_interruption_after_each_named_stage_converges_after_two_reopens(
    tmp_path: Path, stop_stage: str
) -> None:
    path = tmp_path / f"interrupted-{stop_stage}.db"

    class InterruptedDatabase(Database):
        def _remote_schema_stage_hook(self, stage: str) -> None:
            if stage == stop_stage:
                raise RuntimeError(f"stop after {stage}")

    with pytest.raises(RuntimeError, match="stop after"):
        InterruptedDatabase(path)
    Database(path).close()
    Database(path).close()
    assert REMOTE_TABLES <= _objects(path, "table").keys()
    assert REMOTE_INDEXES <= _objects(path, "index").keys()


@pytest.mark.parametrize(
    ("kind", "name", "replacement"),
    [
        ("table", "remote_runners", "CREATE TABLE remote_runners(id TEXT PRIMARY KEY, wrong TEXT)"),
        (
            "table",
            "remote_runner_workspaces",
            TABLE_SQL["remote_runner_workspaces"].replace(
                "UNIQUE(id, runner_id, runner_generation, generation)",
                "UNIQUE(id, runner_id, generation)",
            ),
        ),
        ("index", "remote_one_live_workspace", "CREATE UNIQUE INDEX remote_one_live_workspace ON remote_runner_workspaces(agent_name, runner_id, runner_generation) WHERE state <> 'retired'"),
        (
            "index",
            "remote_one_live_attempt_per_job",
            INDEX_SQL["remote_one_live_attempt_per_job"].replace(
                "state <> 'terminal'", "state = 'running'"
            ),
        ),
        ("index", "remote_reuse_lookup", "CREATE INDEX remote_reuse_lookup ON remote_pre_run_observations(runner_id, workspace_id, runner_generation, workspace_generation, pre_run_digest, exclusions_policy_digest, observation_digest) WHERE complete=1 AND reusable=1"),
    ],
)
def test_conflicting_exact_shapes_fail_before_migration_mutation(
    tmp_path: Path, kind: str, name: str, replacement: str
) -> None:
    path = tmp_path / f"conflict-{name}.db"
    Database(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP {kind.upper()} {name}")
        conn.execute(replacement)
        conn.commit()
    before = _snapshot(path)
    with pytest.raises(sqlite3.DatabaseError, match="conflicting remote-job"):
        Database(path)
    assert _snapshot(path) == before


def test_v0_script_requests_and_remote_conflict_refuse_before_any_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v0-script-requests-remote-conflict.db"
    _seed_legacy_scripts_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE remote_runners(id TEXT PRIMARY KEY, wrong TEXT)"
        )
        conn.execute(
            "INSERT INTO remote_runners(id, wrong) VALUES "
            "('RUNNER-conflict', 'must-survive')"
        )
        conn.commit()

    before = _complete_snapshot(path)
    assert "script_requests" in before[1]
    assert "jobs" not in before[1]
    assert [row[0] for row in before[1]["script_requests"]] == [
        "SR-001", "SR-002", "SR-003",
    ]
    assert before[1]["script_requests"][0][12] == (
        "/runtime/orgs/sample/scripts/SR-001.out"
    )

    for _ in range(2):
        with pytest.raises(
            sqlite3.DatabaseError,
            match="conflicting remote-job table: remote_runners",
        ):
            Database(path)
        assert _complete_snapshot(path) == before


def test_conflicting_nullable_job_column_fails_without_other_remote_writes(tmp_path: Path) -> None:
    path = tmp_path / "column-conflict.db"
    db = Database(path)
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE jobs RENAME COLUMN execution_backend TO old_execution_backend")
        conn.execute("ALTER TABLE jobs ADD COLUMN execution_backend INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "DELETE FROM remote_runner_schema_migrations WHERE name=?",
            (MIGRATION_NAME,),
        )
        conn.commit()
    before = _snapshot(path)
    with pytest.raises(sqlite3.DatabaseError, match="conflicting jobs column"):
        Database(path)
    assert _snapshot(path) == before


def test_live_workspace_uniqueness_and_retire_then_replace(tmp_path: Path) -> None:
    db = Database(tmp_path / "workspace.db")
    _insert_runner(db._conn, "RUNNER-1")
    _insert_workspace(db._conn, "RWS-1", "RUNNER-1")
    db._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_workspace(db._conn, "RWS-2", "RUNNER-1")
    db._conn.rollback()
    db.execute(
        "UPDATE remote_runner_workspaces SET state='retired', retired_at=? WHERE id='RWS-1'",
        ("2026-01-02T00:00:00Z",),
    )
    _insert_workspace(db._conn, "RWS-2", "RUNNER-1")
    db._conn.commit()
    assert db.execute(
        "SELECT id FROM remote_runner_workspaces WHERE runner_id=? AND runner_generation=? "
        "AND agent_name=? AND state <> 'retired'",
        ("RUNNER-1", 1, "dev_agent"),
    ).fetchall()[0][0] == "RWS-2"


def test_partial_duplicate_live_workspaces_refuse_on_two_reopens_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate-partial.db"
    Database(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX remote_one_live_workspace")
        conn.execute(
            "UPDATE remote_runner_schema_migrations SET stage='create_runner_tables' WHERE name=?",
            (MIGRATION_NAME,),
        )
        conn.execute("PRAGMA foreign_keys=ON")
        _insert_runner(conn, "RUNNER-1")
        _insert_workspace(conn, "RWS-1", "RUNNER-1")
        _insert_workspace(conn, "RWS-2", "RUNNER-1")
        conn.commit()
    before = _snapshot(path)
    for _ in range(2):
        with pytest.raises(sqlite3.IntegrityError, match="duplicate live"):
            Database(path)
        assert _snapshot(path) == before


def test_composite_foreign_keys_reject_cross_identity_and_generation(tmp_path: Path) -> None:
    db = Database(tmp_path / "composite.db")
    _insert_runner(db._conn, "RUNNER-1")
    _insert_runner(db._conn, "RUNNER-2")
    _insert_workspace(db._conn, "RWS-1", "RUNNER-1")
    _insert_job(db, "JOB-1")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_attempt(db._conn, "RATT-bad-runner", "JOB-1", "RUNNER-2", "RWS-1")
    db._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_attempt(
            db._conn, "RATT-bad-generation", "JOB-1", "RUNNER-1", "RWS-1",
            workspace_generation=2,
        )


def test_reuse_lookup_isolated_by_workspace_agent_runner_and_runner_generation(tmp_path: Path) -> None:
    db = Database(tmp_path / "reuse.db")
    _insert_runner(db._conn, "RUNNER-1", generation=2)
    _insert_runner(db._conn, "RUNNER-2")
    _insert_workspace(db._conn, "RWS-a", "RUNNER-1", runner_generation=2, agent="agent-a")
    _insert_workspace(db._conn, "RWS-b", "RUNNER-1", runner_generation=2, agent="agent-b")
    _insert_workspace(db._conn, "RWS-c", "RUNNER-2", agent="agent-a")
    for suffix, runner, runner_gen, workspace in (
        ("a", "RUNNER-1", 2, "RWS-a"),
        ("b", "RUNNER-1", 2, "RWS-b"),
        ("c", "RUNNER-2", 1, "RWS-c"),
    ):
        _insert_job(db, f"JOB-{suffix}")
        _insert_attempt(db._conn, f"RATT-{suffix}", f"JOB-{suffix}", runner, workspace, runner_generation=runner_gen)
        db._conn.execute(
            "INSERT INTO remote_phase_receipts "
            "(id,attempt_id,phase,ordinal,outcome,receipt_json,receipt_digest,accepted_frame_seq) "
            "VALUES (?,?, 'workspace_observation',1,'succeeded','{}',?,1)",
            (f"RPR-{suffix}", f"RATT-{suffix}", f"receipt-{suffix}"),
        )
        db._conn.execute(
            "INSERT INTO remote_pre_run_observations VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"RPO-{suffix}", f"RATT-{suffix}", f"RPR-{suffix}", runner,
                runner_gen, workspace, 1, "same-pre", 1, "same-policy", "[]",
                "[]", "{}", "same-observation", 1, 1, "2026-01-01T00:00:00Z",
            ),
        )
    db._conn.commit()
    rows = db.execute(
        "SELECT id FROM remote_pre_run_observations WHERE runner_id=? AND runner_generation=? "
        "AND workspace_id=? AND workspace_generation=? AND pre_run_digest=? "
        "AND exclusions_policy_digest=? AND observation_digest=? AND complete=1 AND reusable=1",
        ("RUNNER-1", 2, "RWS-a", 1, "same-pre", "same-policy", "same-observation"),
    ).fetchall()
    assert [row[0] for row in rows] == ["RPO-a"]


def test_existing_local_job_values_and_overloaded_references_survive(tmp_path: Path) -> None:
    path = tmp_path / "compat.db"
    db = Database(path)
    db.execute(
        "INSERT INTO tasks(id,status,brief,created_at,updated_at,blocked_on_job_ids) "
        "VALUES ('TASK-1','blocked','b','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','[\"JOB-1\"]')"
    )
    _insert_job(db, "JOB-1")
    db.execute("UPDATE jobs SET status='running', reason='local-reason' WHERE id='JOB-1'")
    db.execute(
        "INSERT INTO audit_log(task_id,agent,action,payload,timestamp) "
        "VALUES ('config:working_hours','founder','legacy','{}','2026-01-01T00:00:00Z')"
    )
    db._conn.commit()
    before = tuple(db.execute("SELECT status,reason FROM jobs WHERE id='JOB-1'").fetchone())
    db.close()
    Database(path).close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status,reason FROM jobs WHERE id='JOB-1'").fetchone() == before
        assert conn.execute("SELECT blocked_on_job_ids FROM tasks WHERE id='TASK-1'").fetchone() == ('[\"JOB-1\"]',)
        assert conn.execute("SELECT task_id FROM audit_log WHERE action='legacy'").fetchone() == ("config:working_hours",)
        remote_values = conn.execute(
            "SELECT execution_backend,selected_runner_id,remote_bundle_json,remote_bundle_digest,current_remote_attempt_id "
            "FROM jobs WHERE id='JOB-1'"
        ).fetchone()
        assert remote_values == (None, None, None, None, None)


@pytest.mark.parametrize(
    ("family", "commit"),
    [("v0", SCRIPT_REQUEST_V0_COMMIT), ("v1", SCRIPT_REQUEST_V1_COMMIT)],
)
def test_authentic_historical_script_request_families_converge_and_preserve_values(
    tmp_path: Path, family: str, commit: str,
) -> None:
    """Provenance: v0 initial DDL commit; v1 exact pre-jobs parent tree."""
    path = tmp_path / f"script-{family}.db"
    _build_authentic_script_request_store(path, tmp_path / f"source-{family}", commit)
    before = sqlite3.connect(path).execute(
        "SELECT * FROM script_requests WHERE id='SR-901'"
    ).fetchone()

    Database(path).close()
    Database(path).close()
    Database(path).close()

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT id,task_id,agent_name,title,rationale,script_text,interpreter,"
            "cwd_hint,status,exit_code,stdout_head,stderr_head,stdout_path,stderr_path,"
            "duration_ms,started_at,finished_at,reviewed_at,reviewed_by,reject_reason,"
            "cwd_resolved,max_runtime_seconds,created_at FROM jobs WHERE id='JOB-901'"
        ).fetchone() == (
            "JOB-901", *before[1:12],
            "/runtime/orgs/sample/jobs/JOB-901.out",
            "/runtime/orgs/sample/jobs/JOB-901.err",
            *before[14:21], before[21], before[22],
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT stage FROM remote_runner_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION_NAME,),
        ).fetchone() == (COMPLETE_STAGE,)


def test_exact_untouched_merged_s2_upgrades_and_preserves_every_unrelated_byte_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "untouched-s2.db"
    _build_exact_historical_s2(path, tmp_path / "historical-source")
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO tasks(id,status,brief,created_at,updated_at,blocked_on_job_ids) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("TASK-s2", "blocked", "legacy", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z", '["JOB-s2-modern","JOB-s2-v1","JOB-s2-v0"]'),
            ],
        )
        conn.executemany(
            "INSERT INTO jobs(id,task_id,agent_name,title,rationale,script_text,"
            "interpreter,status,reason,stdout_path,stderr_path,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("JOB-s2-modern", "TASK-s2", "dev_agent", "modern", "why", "true",
                 "bash", "completed", "complete", "/jobs/modern.out", "/jobs/modern.err",
                 "2026-01-01T00:00:00Z"),
                ("JOB-s2-v1", "TASK-s2", "dev_agent", "v1", "why", "false",
                 "bash", "rejected", "founder_rejected", "/jobs/v1.out", "/jobs/v1.err",
                 "2026-01-02T00:00:00Z"),
                ("JOB-s2-v0", "TASK-s2", "dev_agent", "v0-script-request-history",
                 "why", "exit 7", "bash", "failed", "daemon_crash",
                 "/scripts/SR-legacy.out", "/scripts/SR-legacy.err",
                 "2026-01-03T00:00:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO audit_log(task_id,agent,action,payload,timestamp) VALUES "
            "(?,?,?,?,?)",
            [
                ("config:working_hours", "founder", "legacy", "{}", "2026-01-01T00:00:00Z"),
                ("TASK-s2", "dev_agent", "job_failed", '{"job_id":"JOB-s2-v0"}',
                 "2026-01-03T00:01:00Z"),
            ],
        )
        conn.commit()
    before_schema, before_rows = _complete_snapshot(path)

    Database(path).close()
    Database(path).close()
    Database(path).close()

    after_schema, after_rows = _complete_snapshot(path)
    added = {
        "remote_runner_enrollment_challenges",
        "remote_enrollment_challenge_expiry",
    }
    def unrelated(schema: list[tuple]) -> list[tuple]:
        return [
            row for row in schema
            if row[1] not in added | {"remote_runners"}
            and row[2] not in {"remote_runners", "remote_runner_enrollment_challenges"}
        ]
    assert unrelated(after_schema) == unrelated(before_schema)
    for table, rows in before_rows.items():
        if table not in {"remote_runners", "remote_runner_schema_migrations"}:
            assert after_rows[table] == rows
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT name,stage FROM remote_runner_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION_NAME,),
        ).fetchall() == [(IDENTITY_MIGRATION_NAME, COMPLETE_STAGE)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (IDENTITY_TEMP_PARENT,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT blocked_on_job_ids FROM tasks WHERE id='TASK-s2'"
        ).fetchone() == ('["JOB-s2-modern","JOB-s2-v1","JOB-s2-v0"]',)
        assert conn.execute(
            "SELECT task_id FROM audit_log WHERE action='legacy'"
        ).fetchone() == ("config:working_hours",)


@pytest.mark.parametrize(
    "stop_point",
    [point for stage in IDENTITY_STAGES for point in (f"before:{stage}", stage)]
    + ["before:parent_replacement", "after:parent_replacement"],
)
def test_identity_interruption_before_and_after_every_stage_converges_twice(
    tmp_path: Path, stop_point: str,
) -> None:
    path = tmp_path / f"identity-{stop_point.replace(':', '-')}.db"
    replacement_boundary = stop_point.endswith("parent_replacement")
    before = None
    if replacement_boundary:
        _build_exact_historical_s2(path, tmp_path / "historical-source")
        # Resume at the shipping stage immediately before the rebuild.  The
        # first stage is already durably committed, so the boundary hook's
        # transaction is the only transaction under test here.
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO remote_runner_schema_migrations(name,stage,updated_at) "
                "VALUES (?,?,?)",
                (IDENTITY_MIGRATION_NAME, IDENTITY_STAGES[0], "2026-01-01T00:00:00Z"),
            )
            conn.commit()
        before = _complete_snapshot(path)

    class InterruptedDatabase(Database):
        def _remote_identity_schema_stage_hook(self, point: str) -> None:
            if point == stop_point:
                raise RuntimeError(f"stop at {point}")

    with pytest.raises(RuntimeError, match="stop at"):
        InterruptedDatabase(path)
    if replacement_boundary:
        # Both hooks execute inside the shipping BEGIN IMMEDIATE transaction;
        # even the post-rename hook must roll the DROP/RENAME back byte/value-exact.
        assert _complete_snapshot(path) == before
        with sqlite3.connect(path) as conn:
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (IDENTITY_TEMP_PARENT,)
            ).fetchone() is None
    Database(path).close()
    Database(path).close()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT stage FROM remote_runner_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION_NAME,),
        ).fetchone() == (COMPLETE_STAGE,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (IDENTITY_TEMP_PARENT,)
        ).fetchone() is None


def test_complete_validation_and_marker_publication_share_one_locked_transaction(
    tmp_path: Path,
) -> None:
    """Production-open proof: no writer can enter between proof and marker."""
    path = tmp_path / "atomic-complete.db"

    class StopAfterValidation(Database):
        def _remote_identity_schema_stage_hook(self, point: str) -> None:
            if point != "after:complete_validation":
                return
            assert self._conn.in_transaction
            contender = sqlite3.connect(path, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute(
                        "UPDATE remote_runner_schema_migrations SET updated_at='raced'"
                    )
            finally:
                contender.close()
            raise RuntimeError("stop after complete validation")

    with pytest.raises(RuntimeError, match="stop after complete validation"):
        StopAfterValidation(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT stage FROM remote_runner_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION_NAME,),
        ).fetchone() == ("validate_identity_enrollment_schema",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (IDENTITY_TEMP_PARENT,)
        ).fetchone() is None

    Database(path).close()
    Database(path).close()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT stage FROM remote_runner_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION_NAME,),
        ).fetchone() == (COMPLETE_STAGE,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("case_id", "kind", "name", "replacement"),
    IDENTITY_DDL_DRIFT_CASES,
    ids=[case[0] for case in IDENTITY_DDL_DRIFT_CASES],
)
def test_identity_exact_shape_drift_refuses_before_any_mutation(
    tmp_path: Path, case_id: str, kind: str, name: str, replacement: str,
) -> None:
    path = tmp_path / f"identity-drift-{case_id.replace(':', '-')}.db"
    Database(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(f"DROP {kind.upper()} {name}")
        conn.execute(replacement)
        conn.commit()
    before = _complete_snapshot(path)
    for _ in range(2):
        with pytest.raises(sqlite3.DatabaseError):
            Database(path)
        assert _complete_snapshot(path) == before


@pytest.mark.parametrize("table", [
    "remote_runners", "remote_runner_workspaces", "remote_job_attempts",
    "remote_phase_receipts", "remote_pre_run_observations", "remote_protocol_frames",
    "remote_runner_enrollment_challenges",
])
def test_untouched_s2_with_any_runner_graph_row_refuses_without_mutation(
    tmp_path: Path, table: str,
) -> None:
    path = tmp_path / f"nonempty-{table}.db"
    _downgrade_to_exact_untouched_s2(path)
    with sqlite3.connect(path) as conn:
        _insert_runner_s2(conn, "RUNNER-x")
        if table != "remote_runners":
            _insert_workspace(conn, "RWS-x", "RUNNER-x")
        if table in {
            "remote_job_attempts", "remote_phase_receipts",
            "remote_pre_run_observations", "remote_protocol_frames",
        }:
            conn.execute(
                "INSERT INTO jobs(id,task_id,agent_name,title,rationale,script_text,"
                "interpreter,created_at) VALUES "
                "('JOB-x','TASK-x','dev_agent','x','x','true','bash','2026-01-01Z')"
            )
            _insert_attempt(conn, "RATT-x", "JOB-x", "RUNNER-x", "RWS-x")
        if table in {"remote_phase_receipts", "remote_pre_run_observations"}:
            conn.execute(
                "INSERT INTO remote_phase_receipts "
                "(id,attempt_id,phase,ordinal,outcome,receipt_json,receipt_digest,accepted_frame_seq) "
                "VALUES ('RPR-x','RATT-x','workspace_observation',1,'succeeded','{}','digest',1)"
            )
        if table == "remote_pre_run_observations":
            conn.execute(
                "INSERT INTO remote_pre_run_observations VALUES "
                "('RPO-x','RATT-x','RPR-x','RUNNER-x',1,'RWS-x',1,'pre',1,'policy',"
                "'[]','[]','{}','observation',1,1,'2026-01-01Z')"
            )
        if table == "remote_protocol_frames":
            conn.execute(
                "INSERT INTO remote_protocol_frames VALUES "
                "('RATT-x','connection',1,'terminal','digest','accepted','2026-01-01Z')"
            )
        if table == "remote_runner_enrollment_challenges":
            conn.execute(TABLE_SQL["remote_runner_enrollment_challenges"])
            conn.execute(
                "INSERT INTO remote_runner_enrollment_challenges "
                "(id,org_slug,token_fingerprint,challenge_nonce,display_name,"
                "attestation_json,attestation_digest,ceremony_kind,"
                "target_runner_generation,expires_at,created_at) VALUES "
                "('RENC-x','sample','token','nonce','runner','{}','att',"
                "'initial',1,'2026-01-02Z','2026-01-01Z')"
            )
        conn.commit()
    before = _complete_snapshot(path)
    with pytest.raises(sqlite3.DatabaseError):
        Database(path)
    assert _complete_snapshot(path) == before


def test_enrollment_challenge_exact_checks_uniques_and_foreign_keys(tmp_path: Path) -> None:
    db = Database(tmp_path / "challenge-ddl.db")
    _insert_runner(db._conn, "RUNNER-1")
    base = (
        "RENC-1", "sample", "token-1", "nonce-1", "runner", "{}", "att",
        "initial", None, 1, "2026-01-02Z", "2026-01-01Z", None, None,
        None, None, None, None, None, None,
    )
    db._conn.execute(
        "INSERT INTO remote_runner_enrollment_challenges VALUES (" + ",".join("?" * 20) + ")",
        base,
    )
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO remote_runner_enrollment_challenges VALUES (" + ",".join("?" * 20) + ")",
            ("RENC-2", *base[1:2], "token-1", *base[3:]),
        )
    db._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO remote_runner_enrollment_challenges VALUES (" + ",".join("?" * 20) + ")",
            ("RENC-bad", "sample", "token-bad", "nonce-bad", "runner", "{}", "att",
             "generation_rotation", "RUNNER-1", 1, "2026-01-02Z", "2026-01-01Z",
             None, None, None, None, None, None, None, None),
        )


PINNED_TASK_6611_CONTRACT = r'''# TASK-6611 — corrected identity/enrollment persistence amendment

## 2. Exact canonical amended DDL
```sql
CREATE TABLE remote_runners (
  id TEXT PRIMARY KEY,
  org_slug TEXT NOT NULL,
  display_name TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation >= 1),
  state TEXT NOT NULL CHECK(state IN ('unavailable','available','busy','draining','revoked','unhealthy')),
  capacity INTEGER NOT NULL DEFAULT 1 CHECK(capacity = 1),
  protocol_min INTEGER NOT NULL DEFAULT 1,
  protocol_max INTEGER NOT NULL DEFAULT 1,
  capabilities_json TEXT NOT NULL,
  attestation_json TEXT NOT NULL,
  attestation_digest TEXT NOT NULL,
  attested_at TEXT NOT NULL,
  attestation_expires_at TEXT NOT NULL,
  cert_serial TEXT NOT NULL,
  cert_spki_sha256 TEXT NOT NULL,
  cert_expires_at TEXT NOT NULL,
  revocation_epoch INTEGER NOT NULL DEFAULT 0 CHECK(revocation_epoch >= 0),
  last_seen_at TEXT,
  unavailable_reason TEXT,
  unhealthy_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  revoked_at TEXT,
  revoke_reason TEXT,
  UNIQUE(org_slug, display_name),
  UNIQUE(org_slug, cert_serial),
  UNIQUE(org_slug, cert_spki_sha256)
);
CREATE TABLE remote_runner_enrollment_challenges (
  id TEXT PRIMARY KEY,
  org_slug TEXT NOT NULL,
  token_fingerprint TEXT NOT NULL,
  challenge_nonce TEXT NOT NULL,
  display_name TEXT NOT NULL,
  attestation_json TEXT NOT NULL,
  attestation_digest TEXT NOT NULL,
  ceremony_kind TEXT NOT NULL CHECK(ceremony_kind IN ('initial','generation_rotation')),
  target_runner_id TEXT,
  target_runner_generation INTEGER NOT NULL CHECK(target_runner_generation >= 1),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT,
  revoked_at TEXT,
  revoke_reason TEXT,
  csr_digest TEXT,
  runner_id TEXT,
  runner_generation INTEGER CHECK(runner_generation IS NULL OR runner_generation >= 1),
  enrollment_receipt_json TEXT,
  enrollment_receipt_digest TEXT,
  CHECK((revoked_at IS NULL) = (revoke_reason IS NULL)),
  CHECK((ceremony_kind = 'initial' AND target_runner_id IS NULL AND target_runner_generation = 1) OR (ceremony_kind = 'generation_rotation' AND target_runner_id IS NOT NULL AND target_runner_generation > 1)),
  CHECK(ceremony_kind = 'initial' OR runner_id IS NULL OR runner_id = target_runner_id),
  CHECK((consumed_at IS NULL AND csr_digest IS NULL AND runner_id IS NULL AND runner_generation IS NULL AND enrollment_receipt_json IS NULL AND enrollment_receipt_digest IS NULL) OR (consumed_at IS NOT NULL AND csr_digest IS NOT NULL AND runner_id IS NOT NULL AND runner_generation = target_runner_generation AND enrollment_receipt_json IS NOT NULL AND enrollment_receipt_digest IS NOT NULL)),
  UNIQUE(token_fingerprint),
  UNIQUE(org_slug, challenge_nonce),
  FOREIGN KEY(runner_id) REFERENCES remote_runners(id),
  FOREIGN KEY(target_runner_id) REFERENCES remote_runners(id)
);
CREATE INDEX remote_enrollment_challenge_expiry ON remote_runner_enrollment_challenges(org_slug, expires_at) WHERE consumed_at IS NULL AND revoked_at IS NULL;
```

## 4. Exact migration/preflight contract
Use `remote_runner_schema_migrations` with marker name `generic_remote_runner_identity_enrollment_v1` and only these stages, in order:
1. `validate_empty_s2_runner_graph`
2. `rebuild_remote_runners_with_cert_expiry`
3. `create_enrollment_challenges`
4. `create_enrollment_expiry_index`
5. `validate_identity_enrollment_schema`
6. `complete`
Validation covers normalized `sqlite_master` SQL plus exact column order/type/null/default/PK, foreign keys, CHECKs, unique constraints, index column/order/predicate, absence of the reserved temporary parent, the single marker row, and allowed stage/order.
Unknown, duplicate, out-of-order, or conflicting marker/object state aborts before mutation.
The exact untouched merged-S2 shape from commit `1be72fe71a779eb3393b9c10dcfeae8a487d3f78` is accepted only while the marker is absent/incomplete and the entire runner graph is empty; the exact canonical amended shape is accepted regardless of rows. Once complete only canonical is accepted.
The reserved temporary parent is `remote_runners_identity_enrollment_v1_new`.

## 5. Mandatory fixtures and acceptance matrix
Separate synthetic fixtures must interrupt immediately before/after every stage and parent replacement, and construct each allowed partial stage; two reopens converge to the same canonical result. Negative fixtures mutate every table/column order/type/null/default/PK/CHECK/unique/FK/index order/predicate and marker state, add temp residue, and place a row in each graph/identity table in turn; every case refuses before mutation and preserves a complete schema/row snapshot.

## 6. Bidirectional traceability
| Requirement / display / behavior | Durable authority and proof |
|---|---|
| single-use token; 15-minute first-use bound | token_fingerprint, expires_at, consumed_at, revoked_at; unconsumed-only expiry index |
| restart-safe exact replay/conflict | nonce, CSR digest, immutable receipt JSON/digest; consumed branch before first-use clocks |
| initial vs rotation identity | ceremony kind/target columns, result runner columns, row CHECKs, BEGIN IMMEDIATE current-generation validation |
| posture, assertor, attestation expiry, network policy | schema-validated canonical v1 attestation_json and attestation_digest |
| current certificate serial/fingerprint/expiry | current runner serial/SPKI and new NOT NULL expiry column |
| stale certificate denial | current serial/SPKI/expiry plus existing generation/revocation epoch |
| revocation cleanup visibility | canonical state='revoked'; subordinate bounded unhealthy reason |
| enrollment audit/restart receipt | immutable stored receipt plus existing audit evidence; audit is not authority |
| safe shipped-S2 transition | named marker/stages, exact two-shape preflight, empty-graph proof, atomic rebuild |
| compatibility | untouched-S2, historical, interruption, conflict, complete-snapshot, FK and repeat-open fixtures |
'''


def _split_contract_sql_items(body: str) -> tuple[str, ...]:
    items: list[str] = []
    start = 0
    depth = 0
    quoted = False
    for offset, character in enumerate(body):
        if character == "'":
            quoted = not quoted
        elif not quoted and character == "(":
            depth += 1
        elif not quoted and character == ")":
            depth -= 1
        elif not quoted and character == "," and depth == 0:
            items.append(body[start:offset].strip())
            start = offset + 1
    items.append(body[start:].strip())
    return tuple(item for item in items if item)



def _parse_section_6_traceability() -> tuple[tuple[str, str], ...]:
    section = PINNED_TASK_6611_CONTRACT.split("## 6. Bidirectional traceability", 1)[1]
    rows = tuple(
        (cells[0].strip(), cells[1].strip())
        for line in section.splitlines()
        if line.startswith("|")
        and (cells := line.strip("|").split("|"))
        and not cells[0].strip().startswith(("---", "Requirement"))
    )
    assert len(rows) == 10
    return rows


def _section_6_schema_artifacts() -> tuple[str, ...]:
    """Parse the persistence artifact universe independently from production."""
    sql_block = re.search(r"```sql\n(.*?)```", PINNED_TASK_6611_CONTRACT, re.S)
    assert sql_block is not None
    artifacts: list[str] = []
    for statement in re.split(r";\s*(?=CREATE|$)", sql_block.group(1)):
        statement = statement.strip()
        if not statement:
            continue
        table_match = re.match(r"CREATE TABLE (\w+) \((.*)\)$", statement, re.S)
        if table_match:
            table, body = table_match.groups()
            artifacts.append(f"table:{table}")
            ordinal = 0
            for item in _split_contract_sql_items(body):
                normalized = " ".join(item.split())
                column = re.match(r"([a-z]\w*) (TEXT|INTEGER)\b(.*)", normalized)
                if column:
                    name, declared_type, suffix = column.groups()
                    default = re.search(r"DEFAULT ([^ ,)]+)", suffix)
                    artifacts.append(
                        f"column:{table}:{ordinal}:{name}:{declared_type}:"
                        f"null={int('NOT NULL' in suffix)}:"
                        f"default={default.group(1) if default else '-'}:"
                        f"pk={int('PRIMARY KEY' in suffix)}"
                    )
                    ordinal += 1
                if normalized.startswith(("CHECK(", "UNIQUE(", "FOREIGN KEY(")):
                    artifacts.append(f"constraint:{table}:{normalized}")
            continue
        index = re.match(
            r"CREATE (UNIQUE )?INDEX (\w+) ON (\w+)\((.*?)\)(?:\s+WHERE (.*))?$",
            " ".join(statement.split()),
        )
        assert index is not None
        unique, name, table, columns, predicate = index.groups()
        artifacts.append(
            f"index:{name}:unique={int(unique is not None)}:table={table}:"
            f"columns={columns.replace(' ', '')}:predicate={predicate or '-'}"
        )
    stages = tuple(re.findall(r"^\d+\. `([^`]+)`$", PINNED_TASK_6611_CONTRACT, re.M))
    artifacts.extend((
        "marker-field:name", "marker-field:stage", "marker-field:updated_at",
        "marker-name:generic_remote_runner_identity_enrollment_v1",
        *(f"stage:{ordinal}:{stage}" for ordinal, stage in enumerate(stages)),
    ))
    compatibility = dict(_parse_section_6_traceability())["compatibility"]
    artifacts.extend(
        f"fixture:{item.strip().replace(' ', '-')}"
        for item in compatibility.split(",")
    )
    assert len(artifacts) == len(set(artifacts))
    return tuple(artifacts)


def test_task_6611_section_6_executable_bidirectional_traceability(tmp_path: Path) -> None:
    """Every approved Section 6 row has resolved, executed schema evidence."""
    trace_rows = _parse_section_6_traceability()
    requirements = tuple(requirement for requirement, _ in trace_rows)
    artifacts = _section_6_schema_artifacts()
    deferred = {
        "restart-safe exact replay/conflict",
        "stale certificate denial",
        "revocation cleanup visibility",
        "enrollment audit/restart receipt",
    }
    assert deferred < set(requirements)

    def owner(artifact: str) -> str:
        if artifact.startswith("fixture:"):
            return "compatibility"
        if artifact.startswith(("marker-", "marker-field:", "stage:")):
            return "safe shipped-S2 transition"
        if artifact.startswith("index:remote_enrollment_challenge_expiry"):
            return "single-use token; 15-minute first-use bound"
        if artifact.startswith("constraint:remote_runner_enrollment_challenges:CHECK((consumed_at"):
            return "restart-safe exact replay/conflict"
        if artifact.startswith("constraint:remote_runner_enrollment_challenges:"):
            return "initial vs rotation identity"
        if artifact.startswith("table:remote_runner_enrollment_challenges"):
            return "restart-safe exact replay/conflict"
        if artifact.startswith("column:remote_runner_enrollment_challenges:"):
            column = artifact.split(":", 4)[3]
            if column in {"token_fingerprint", "expires_at", "consumed_at", "revoked_at"}:
                return "single-use token; 15-minute first-use bound"
            if column in {
                "challenge_nonce", "csr_digest", "enrollment_receipt_json",
                "enrollment_receipt_digest",
            }:
                return "restart-safe exact replay/conflict"
            if column in {
                "ceremony_kind", "target_runner_id", "target_runner_generation",
                "runner_id", "runner_generation",
            }:
                return "initial vs rotation identity"
            if column in {"attestation_json", "attestation_digest"}:
                return "posture, assertor, attestation expiry, network policy"
            return "enrollment audit/restart receipt"
        if artifact.startswith(("table:remote_runners", "constraint:remote_runners:")):
            return "current certificate serial/fingerprint/expiry"
        if artifact.startswith("column:remote_runners:"):
            column = artifact.split(":", 4)[3]
            if column in {"attestation_json", "attestation_digest", "attestation_expires_at"}:
                return "posture, assertor, attestation expiry, network policy"
            if column in {"generation", "cert_serial", "cert_spki_sha256", "cert_expires_at", "revocation_epoch"}:
                return "stale certificate denial"
            if column in {"state", "unhealthy_reason", "revoked_at", "revoke_reason"}:
                return "revocation cleanup visibility"
            return "current certificate serial/fingerprint/expiry"
        raise AssertionError(f"unmapped approved schema artifact: {artifact}")

    artifact_to_requirement = {artifact: owner(artifact) for artifact in artifacts}
    assert set(artifact_to_requirement) == set(artifacts)
    assert set(artifact_to_requirement.values()) <= set(requirements)

    def prove_exact_schema() -> frozenset[str]:
        path = tmp_path / "section-6.db"
        Database(path).close()
        with sqlite3.connect(path) as conn:
            actual_sql = {
                name: conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                for name in APPROVED_DDL_SHA256
            }
        assert set(actual_sql) == set(APPROVED_DDL_SHA256)
        actual_digests = {
            name: hashlib.sha256(_normalized_ddl(sql).encode()).hexdigest()
            for name, sql in actual_sql.items()
        }
        assert actual_digests == APPROVED_DDL_SHA256
        assert all(
            artifact.split(":", 2)[1] in actual_sql
            for artifact in artifacts
            if artifact.startswith(("table:", "index:"))
        )
        return frozenset({"database-open", "actual-vs-approved-ddl", "artifact-totality"})

    def prove_fresh() -> frozenset[str]:
        proof_root = tmp_path / "fresh"
        proof_root.mkdir()
        test_fresh_database_has_exact_s2_schema_and_nullable_job_columns(proof_root)
        return frozenset({"fresh-open", "canonical", "foreign-key-check"})

    def prove_historical(family: str, commit: str) -> frozenset[str]:
        proof_root = tmp_path / family
        proof_root.mkdir()
        test_authentic_historical_script_request_families_converge_and_preserve_values(
            proof_root, family, commit,
        )
        return frozenset({f"historical-{family}", "all-row-preservation", "two-reopens"})

    def prove_modern() -> frozenset[str]:
        proof_root = tmp_path / "modern"
        proof_root.mkdir()
        test_existing_local_job_values_and_overloaded_references_survive(proof_root)
        return frozenset({"modern", "all-row-preservation", "overloaded-semantics"})

    def prove_untouched_s2() -> frozenset[str]:
        proof_root = tmp_path / "untouched-s2"
        proof_root.mkdir()
        test_exact_untouched_merged_s2_upgrades_and_preserves_every_unrelated_byte_value(
            proof_root
        )
        return frozenset({
            "untouched-s2", "sqlite-master-preservation", "all-row-preservation",
            "foreign-key-check", "no-residue", "two-reopens", "canonical",
        })

    def prove_stages() -> frozenset[str]:
        assert tuple(IDENTITY_STAGES) == tuple(
            artifact.split(":", 2)[2]
            for artifact in artifacts if artifact.startswith("stage:")
        )
        return frozenset({"approved-stage-order"})

    def deferred_s3() -> frozenset[str]:
        return frozenset({"explicitly-deferred"})

    registry = {
        "schema.actual": prove_exact_schema,
        "migration.stages": prove_stages,
        "fixtures.fresh": prove_fresh,
        "fixtures.historical-v0": lambda: prove_historical("v0", SCRIPT_REQUEST_V0_COMMIT),
        "fixtures.historical-v1": lambda: prove_historical("v1", SCRIPT_REQUEST_V1_COMMIT),
        "fixtures.modern": prove_modern,
        "fixtures.untouched-s2": prove_untouched_s2,
        "deferred.s3-lifecycle": deferred_s3,
    }

    requirement_to_proofs = {
        requirement: (
            ("deferred.s3-lifecycle",) if requirement in deferred
            else ("migration.stages", "fixtures.untouched-s2")
            if requirement == "safe shipped-S2 transition"
            else (
                "fixtures.fresh", "fixtures.historical-v0", "fixtures.historical-v1",
                "fixtures.modern", "fixtures.untouched-s2",
            ) if requirement == "compatibility"
            else ("schema.actual",)
        )
        for requirement in requirements
    }
    assert set(requirement_to_proofs) == set(requirements)
    assert all(requirement_to_proofs.values())
    referenced = {proof for proofs in requirement_to_proofs.values() for proof in proofs}
    assert referenced == set(registry)
    required_claims = {
        "schema.actual": frozenset({"database-open", "actual-vs-approved-ddl", "artifact-totality"}),
        "migration.stages": frozenset({"approved-stage-order"}),
        "fixtures.fresh": frozenset({"fresh-open", "canonical", "foreign-key-check"}),
        "fixtures.historical-v0": frozenset({"historical-v0", "all-row-preservation", "two-reopens"}),
        "fixtures.historical-v1": frozenset({"historical-v1", "all-row-preservation", "two-reopens"}),
        "fixtures.modern": frozenset({"modern", "all-row-preservation", "overloaded-semantics"}),
        "fixtures.untouched-s2": frozenset({
            "untouched-s2", "sqlite-master-preservation", "all-row-preservation",
            "foreign-key-check", "no-residue", "two-reopens", "canonical",
        }),
        "deferred.s3-lifecycle": frozenset({"explicitly-deferred"}),
    }
    assert set(required_claims) == referenced
    proof_executions = _execute_section_6_proofs(registry, required_claims)
    assert proof_executions == referenced

    requirement_to_artifacts = {
        requirement: tuple(
            artifact for artifact, mapped in artifact_to_requirement.items()
            if mapped == requirement
        )
        for requirement in requirements
    }
    assert all(requirement_to_artifacts.values())
    assert set(requirement_to_artifacts) == set(requirements)


def test_section_6_registry_rejects_uninvoked_or_bypassed_proof() -> None:
    with pytest.raises(AssertionError, match="unresolved proof"):
        _execute_section_6_proofs({}, {"fixtures.untouched-s2": frozenset({"two-reopens"})})
    with pytest.raises(AssertionError, match="bypassed assertions"):
        _execute_section_6_proofs(
            {"fixtures.untouched-s2": lambda: frozenset({"database-open"})},
            {"fixtures.untouched-s2": frozenset({"database-open", "two-reopens"})},
        )


def test_section_6_exact_ddl_rejects_one_actual_approved_digest_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "digest-mismatch.db"
    Database(path).close()
    with sqlite3.connect(path) as conn:
        actual = {
            name: hashlib.sha256(_normalized_ddl(sql).encode()).hexdigest()
            for name, sql in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE name IN (%s)"
                % ",".join("?" * len(APPROVED_DDL_SHA256)),
                tuple(APPROVED_DDL_SHA256),
            )
        }
    mismatched = dict(APPROVED_DDL_SHA256)
    mismatched["remote_runners"] = "0" * 64
    with pytest.raises(AssertionError):
        assert actual == mismatched
