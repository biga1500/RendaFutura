export type Answers = Record<string, string | string[]>;
export type Question = { id:string; number:number; label:string; help?:string; type:"number"|"money"|"select"|"radio"|"checklist"; options?:string[]; min?:number; max?:number; };
export const GROUPS = [
  {title:"Objetivo e horizonte", subtitle:"O que você deseja construir", reference:"CVM nº 30/2021 · art. 3º, § 1º", questions:[
    {id:"goal",number:1,label:"Qual é o principal objetivo do investimento?",type:"select",options:["Planejar minha aposentadoria","Gerar renda complementar","Construir uma reserva","Buscar independência financeira"]},
    {id:"age",number:2,label:"Qual é sua idade atual?",type:"number",min:18,max:100},
    {id:"retirementAge",number:3,label:"Com que idade pretende se aposentar?",type:"number",min:19,max:105},
    {id:"targetIncome",number:4,label:"Qual renda mensal deseja na aposentadoria?",type:"money",min:1,max:1000000,help:"Valor aproximado, em reais de hoje."},
    {id:"withdrawal",number:5,label:"Quando poderá precisar resgatar parte relevante do dinheiro?",type:"select",options:["Em menos de 1 ano","Entre 1 e 3 anos","Entre 3 e 5 anos","Depois de 5 anos"]},
    {id:"initialCapital",number:6,label:"Quanto já tem disponível para esse objetivo?",type:"money",min:0,max:100000000,help:"Uma estimativa é suficiente. Não solicitamos extratos ou comprovantes."},
  ] as Question[]},
  {title:"Situação financeira",subtitle:"Capacidade de investimento e liquidez",reference:"CVM nº 30/2021 · art. 3º, § 2º",questions:[
    {id:"contribution",number:7,label:"Quanto consegue investir por mês?",type:"select",options:["Não consigo aportar no momento","Até R$ 500","De R$ 501 a R$ 1.500","De R$ 1.501 a R$ 3.000","Mais de R$ 3.000"]},
    {id:"reserve",number:8,label:"Sua reserva de emergência cobre quanto tempo de despesas?",type:"radio",options:["Ainda não tenho reserva","Menos de 3 meses","Entre 3 e 6 meses","Mais de 6 meses"]},
    {id:"stability",number:9,label:"Como é a estabilidade da sua renda?",type:"select",options:["Não tenho renda regular","Minha renda varia bastante","Minha renda é relativamente estável"]},
    {id:"liquidity",number:10,label:"É provável precisar sacar antes do prazo planejado?",type:"radio",options:["Muito provável","Talvez, para despesas imprevistas","Pouco provável"]},
    {id:"debt",number:11,label:"Possui dívidas que comprometem sua renda?",type:"select",options:["Sim, comprometem uma parte importante","Tenho dívidas, mas estão sob controle","Não tenho dívidas relevantes"]},
  ] as Question[]},
  {title:"Conhecimento e experiência",subtitle:"Sua familiaridade com investimentos",reference:"CVM nº 30/2021 · art. 3º, § 3º",questions:[
    {id:"products",number:12,label:"Em quais produtos já investiu?",type:"checklist",options:["Ainda não investi","Poupança","Renda fixa (CDB ou Tesouro)","Fundos de investimento","Ações","Fundos imobiliários (FIIs)","ETFs"]},
    {id:"experience",number:13,label:"Há quanto tempo investe além da poupança?",type:"select",options:["Ainda não investi além da poupança","Há menos de 1 ano","Entre 1 e 3 anos","Há mais de 3 anos"]},
    {id:"knowledge",number:14,label:"Você diferencia riscos de mercado, crédito e liquidez?",type:"radio",options:["Ainda não conheço essas diferenças","Entendo parcialmente","Conheço e consigo explicar as diferenças"]},
  ] as Question[]},
  {title:"Preferência de risco",subtitle:"Como você se sente diante de oscilações",reference:"CVM nº 30/2021 · art. 3º, § 1º, II",questions:[
    {id:"reaction",number:15,label:"O que faria se a carteira caísse 15% em um mês?",type:"radio",options:["Resgataria tudo para evitar novas perdas","Resgataria uma parte","Manteria e acompanharia a estratégia","Consideraria investir mais, após reavaliar os riscos"]},
    {id:"preference",number:16,label:"Qual relação entre segurança e retorno prefere?",type:"radio",options:["Preservar o patrimônio, com pouca oscilação","Aceitar pequenas oscilações por um retorno maior","Aceitar oscilações moderadas no longo prazo","Aceitar oscilações elevadas por maior retorno esperado"]},
    {id:"loss",number:17,label:"Qual queda temporária aceitaria sem resgatar?",type:"radio",options:["Até 5%","Até 10%","Até 20%","Mais de 20%"]},
    {id:"scenario",number:18,label:"Qual cenário se aproxima mais da sua preferência?",type:"radio",options:["Baixa oscilação, mesmo com menor retorno esperado","Pequena oscilação e crescimento gradual","Oscilação moderada e maior crescimento esperado","Alta oscilação e maior potencial de crescimento"]},
  ] as Question[]},
];
export const QUESTIONS = GROUPS.flatMap(g => g.questions);
export function validateAnswers(input:unknown, complete=false):Answers {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("Respostas inválidas.");
  const answers:Answers={};
  for (const q of QUESTIONS) {
    const value=(input as Answers)[q.id];
    if (value === undefined || value === "" || (Array.isArray(value)&&!value.length)) {
      if (complete) throw new Error(`Responda a pergunta ${q.number}: ${q.label}`);
      continue;
    }
    if(q.type==="checklist") {
      if(!Array.isArray(value)||value.some(v=>!q.options!.includes(v))||new Set(value).size!==value.length||value.includes("Ainda não investi")&&value.length>1) throw new Error("Selecione produtos válidos.");
      answers[q.id]=value;
    } else if(q.type==="number"||q.type==="money") {
      if(typeof value!=="string"||value.length>16||!Number.isFinite(Number(value))||Number(value)<q.min!||Number(value)>q.max!||q.type==="number"&&!Number.isInteger(Number(value))) throw new Error(`Confira o valor de: ${q.label}`);
      answers[q.id]=value;
    } else {
      if(typeof value!=="string"||!q.options?.includes(value)) throw new Error(`Selecione uma opção válida: ${q.label}`);
      answers[q.id]=value;
    }
  }
  if(answers.age&&answers.retirementAge&&Number(answers.retirementAge)<=Number(answers.age)) throw new Error("A idade de aposentadoria deve ser maior que a idade atual.");
  return answers;
}
const level=(score:number)=>score<=33?0:score<=66?1:2;
const profileNames=["Conservador","Moderado","Arrojado"];
export function classify(answers:Answers) {
  const optionIndex=(id:string)=>QUESTIONS.find(q=>q.id===id)!.options!.indexOf(answers[id] as string);
  const tolerance=Math.round(["reaction","preference","loss","scenario"].reduce((sum,id)=>sum+optionIndex(id),0)/12*100);
  let capacity=Math.round((optionIndex("reserve")+optionIndex("stability")+optionIndex("liquidity")+optionIndex("debt")+(optionIndex("contribution")>0?2:0))/11*100);
  let capLevel=level(capacity);
  const restrictions:string[]=[];
  const restrict=(maximum:number,message:string)=>{capLevel=Math.min(capLevel,maximum);restrictions.push(message);};
  if(optionIndex("reserve")===0)restrict(0,"A ausência de reserva de emergência limita a exposição a risco.");
  if(optionIndex("debt")===0)restrict(0,"Dívidas que comprometem a renda reduzem a capacidade de assumir risco.");
  if(optionIndex("withdrawal")===0)restrict(0,"A necessidade de resgate em menos de um ano exige preservação e liquidez.");
  if(optionIndex("withdrawal")===1)restrict(1,"O prazo de resgate entre um e três anos limita o risco permitido.");
  if(optionIndex("liquidity")===0)restrict(0,"A alta probabilidade de resgate antecipado exige maior liquidez.");
  if(optionIndex("stability")<2)restrict(1,"A renda variável ou sem regularidade limita a capacidade financeira.");
  const knowledge=optionIndex("knowledge");
  if(knowledge<2)restrictions.push("O conhecimento declarado exige explicações adicionais e restrição a produtos complexos.");
  const toleranceLevel=level(tolerance);
  const operational=Math.min(toleranceLevel,capLevel);
  return {profile:profileNames[operational],tolerance,toleranceProfile:profileNames[toleranceLevel],capacity,capacityProfile:profileNames[capLevel],knowledge:["Inicial","Intermediário","Avançado"][knowledge],horizon:Number(answers.retirementAge)-Number(answers.age),restrictions,ruleVersion:"RF-1.0",explanation:operational<toleranceLevel?"Sua capacidade financeira e suas necessidades de liquidez limitaram o perfil, mesmo com maior tolerância declarada.":"O perfil combina sua tolerância a perdas com sua capacidade financeira, respeitando prazo e liquidez."};
}
export type ProfileResult=ReturnType<typeof classify>;
